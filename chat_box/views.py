import math
import re

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from reversion import revisions

from judge import event_poster as event
from judge.caching import cache_wrapper
from judge.models.notification import Notification, NotificationCategory
from judge.models.profile import Organization, get_profile_public_identity
from judge.utils.community import can_use_community_features
from judge.utils.views import generic_message
from chat_box.exceptions import RoomError, RoomPermissionDenied
from chat_box.models import (
    ChatModerationLog,
    Ignore,
    Message,
    MessageReaction,
    Profile,
    Room,
    RoomMute,
    RoomRedirect,
    UserRoom,
    CHAT_REACTIONS,
    CHAT_REACTION_CODES,
    CHAT_REACTION_EMOJI,
    CHAT_REACTION_IMAGES,
    CHAT_REACTION_LABELS,
    get_first_msg_id,
    get_ignored_user_ids,
)
from chat_box.policies import RoomPolicy
from chat_box.selectors import (
    ROOM_LIST_SECTIONS,
    active_room_mute,
    encode_room_list_cursor,
    get_lobby,
    get_membership,
    get_room_page,
    unread_counts_for_memberships,
)
from chat_box.services.events import broadcast_room_event, chat_event_channels
from chat_box.services.moderation import (
    ROOM_MUTE_MAX_DAYS,
    hide_message as hide_room_message,
    mute_member,
)
from chat_box.services.unread import mark_room_read
from chat_box.utils import (
    create_chat_event_grant,
    decrypt_url,
    encrypt_channel,
    get_reactions_summary,
)

CHAT_TEMP_MUTE_CAP_DAYS = 30
REACTION_LIST_PER_TYPE_LIMIT = 10
CHAT_MODERATION_BATCH_SIZE = 500


def clear_expired_chat_mute(profile):
    if not profile.mute or not profile.mute_until:
        return False

    if profile.mute_until > timezone.now():
        return False

    profile.mute = False
    profile.mute_until = None
    profile.mute_reason = ""
    profile.save(update_fields=["mute", "mute_until", "mute_reason"])
    Profile.dirty_cache(profile.id)
    return True


def is_chat_muted(profile):
    clear_expired_chat_mute(profile)
    return profile.mute


def get_temporary_mute_duration_days(profile):
    previous_mutes = ChatModerationLog.objects.filter(
        message__author=profile,
        action="mute_temp",
    ).count()
    return min(previous_mutes + 1, CHAT_TEMP_MUTE_CAP_DAYS)


class ChatView(ListView):
    context_object_name = "message"
    template_name = "chat/chat.html"
    title = _("LQDOJ Chat")

    def __init__(self):
        super().__init__()
        self.room_id = None
        self.room = None
        self.membership = None
        self.messages = None
        self.first_page_size = 20  # only for first request
        self.follow_up_page_size = 50

    def get_queryset(self):
        return self.messages

    def has_next(self):
        msg_id = get_first_msg_id(self.room_id)
        if not msg_id:
            return False
        return Message(id=msg_id) not in self.messages

    def get_message_page(self, last_id, page_size):
        message_ids = list(
            Message.objects.filter(
                hidden=False, room=self.room_id, id__lt=last_id
            ).values_list("id", flat=True)[:page_size]
        )
        if not message_ids:
            return []

        messages_by_id = {
            message.id: message
            for message in Message.objects.filter(id__in=message_ids)
        }
        return [
            messages_by_id[message_id]
            for message_id in message_ids
            if message_id in messages_by_id
        ]

    def get(self, request, *args, **kwargs):
        request_room = kwargs["room_id"]
        redirected_room = False
        page_size = self.follow_up_page_size
        try:
            last_id = int(request.GET.get("last_id"))
        except Exception:
            last_id = 2**63 - 1
            page_size = self.first_page_size
        only_messages = request.GET.get("only_messages")

        try:
            if request_room:
                try:
                    self.room = Room.objects.get(id=request_room)
                except Room.DoesNotExist:
                    canonical_room_id = (
                        RoomRedirect.objects.filter(old_room_id=request_room)
                        .values_list("canonical_room_id", flat=True)
                        .first()
                    )
                    if canonical_room_id is None:
                        raise
                    self.room = Room.objects.get(id=canonical_room_id)
                    redirected_room = True
            else:
                self.room = get_lobby()
            self.membership = get_membership(self.room, request.profile)
            policy = RoomPolicy(
                request.user,
                request.profile,
                self.room,
                self.membership,
            )
            if not policy.can_view():
                if request.GET.get("switch_room") or only_messages:
                    return HttpResponseForbidden()
                return generic_message(
                    request,
                    _("Access denied"),
                    _(
                        "You do not have access to this private room. Ask a room administrator for an invitation link."
                    ),
                    status=403,
                )
        except Room.DoesNotExist:
            return HttpResponseBadRequest()

        if redirected_room and not (request.GET.get("switch_room") or only_messages):
            return HttpResponseRedirect(
                reverse("chat", kwargs={"room_id": self.room.id})
            )

        request_room = self.room.id
        self.room_id = request_room
        self.messages = self.get_message_page(last_id, page_size)
        if request.GET.get("switch_room"):
            context = self.get_context_data(object_list=self.messages)
            message_template_context = dict(context)
            message_template_context.update(
                {
                    "message": context["message_template"],
                    "is_message_template": True,
                }
            )
            return JsonResponse(
                {
                    "room": {
                        "id": self.room.id,
                        "type": self.room.room_type,
                        "channel_kind": self.room.channel_kind or "",
                        "is_archived": context["room_is_archived"],
                        "max_length": 200 if context["is_lobby"] else 5000,
                        "other_user_id": (
                            context["other_user"].id
                            if context.get("other_user")
                            else ""
                        ),
                        "last_message_id": self.room.last_msg_id,
                    },
                    "user": {
                        "is_room_muted": context["is_room_muted"],
                        "can_interact_room": context["can_interact_room"],
                        "can_moderate_chat": (
                            request.user.is_superuser
                            or (
                                self.membership
                                and self.membership.role
                                in (UserRoom.Role.ADMIN, UserRoom.Role.MODERATOR)
                            )
                        ),
                    },
                    "header_html": render_to_string(
                        "chat/room_header.html", context, request=request
                    ),
                    "messages_html": render_to_string(
                        "chat/message_list.html", context, request=request
                    ),
                    "message_template": render_to_string(
                        "chat/message.html",
                        message_template_context,
                        request=request,
                    ),
                }
            )
        if not only_messages:
            return super().get(request, *args, **kwargs)
        return render(
            request,
            "chat/message_list.html",
            {
                "object_list": self.messages,
                "room_object": self.room,
                "has_next": self.has_next(),
                "can_chat": can_use_community_features(request.user, request.profile),
                "can_interact_room": (
                    RoomPolicy(
                        request.user,
                        request.profile,
                        self.room,
                        get_membership(self.room, request.profile),
                    ).can_post()
                    and not is_chat_muted(request.profile)
                    and not active_room_mute(self.room, request.profile, timezone.now())
                    and can_use_community_features(request.user, request.profile)
                ),
                **message_permission_context(
                    self.messages, request.user, request.profile, self.room
                ),
                **reaction_render_context(self.messages, request.profile),
                **reply_render_context(self.messages, request.user),
            },
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["title"] = self.title
        context["last_msg"] = event.last()
        context["status_sections"] = get_status_context(self.request.profile)
        context["room"] = self.room_id
        context["room_object"] = self.room
        context["room_avatar_url"] = self.room.get_avatar_url()
        context["is_lobby"] = self.room.channel_kind == Room.ChannelKind.LOBBY
        context["has_next"] = self.has_next()
        lobby = get_lobby()
        context["unread_count_lobby"] = get_unread_count(lobby, self.request.profile)
        context["lobby_room"] = lobby
        lobby_membership = get_membership(lobby, self.request.profile)
        context["lobby_hidden"] = (
            lobby_membership.is_hidden if lobby_membership else False
        )
        context["is_chat_muted"] = is_chat_muted(self.request.profile)
        context["can_chat"] = can_use_community_features(
            self.request.user, self.request.profile
        )
        context["can_create_channel"] = (
            self.request.user.is_superuser
            or Organization.objects.filter(admins=self.request.profile)
            .exclude(chat_room__isnull=False)
            .exists()
        )
        event_room_ids = {
            item["room"]
            for section in context["status_sections"]
            for item in section["room_list"]
        }
        event_room_ids.add(self.room.id)
        if not context["lobby_hidden"]:
            event_room_ids.add(lobby.id)
        context["chat_event_channels"] = chat_event_channels(
            self.request.profile.id,
            sorted(event_room_ids),
        )
        context["chat_event_grant"] = create_chat_event_grant(
            self.request.profile.id,
            event_room_ids,
            context["chat_event_channels"],
        )
        context.update(reaction_render_context(self.messages, self.request.profile))
        context.update(reply_render_context(self.messages, self.request.user))
        membership = self.membership
        context["room_membership"] = membership
        context["room_policy"] = RoomPolicy(
            self.request.user,
            self.request.profile,
            self.room,
            membership,
        )
        context["room_can_manage"] = context["room_policy"].can_manage()
        context["room_can_view_moderation"] = context[
            "room_policy"
        ].can_view_moderation()
        context["room_is_archived"] = self.room.archived_at is not None
        context["is_room_muted"] = bool(
            active_room_mute(self.room, self.request.profile, timezone.now())
        )
        context["can_interact_room"] = (
            context["can_chat"]
            and not context["is_chat_muted"]
            and not context["is_room_muted"]
            and context["room_policy"].can_post()
        )
        context.update(
            message_permission_context(
                self.messages,
                self.request.user,
                self.request.profile,
                self.room,
                membership,
            )
        )
        if self.room.room_type == Room.Type.DIRECT:
            other_user = self.room.other_user(self.request.profile)
            if other_user:
                context["other_user"] = other_user
                context["is_self_room"] = other_user.id == self.request.profile.id
                context["other_online"] = get_user_online_status(context["other_user"])
                context["is_ignored"] = False
                if not context["is_self_room"]:
                    context["is_ignored"] = Ignore.is_ignored(
                        self.request.profile, context["other_user"]
                    )
        elif self.room.channel_kind == Room.ChannelKind.LOBBY:
            context["online_count"] = get_online_count()
        else:
            context["room_name"] = self.room.name
            context["room_member_count"] = UserRoom.objects.filter(
                room=self.room, state=UserRoom.State.ACTIVE
            ).count()
        context["message_template"] = {
            "author_id": self.request.profile.id,
            "id": "$id",
            "time": timezone.now(),
            "body": "$body",
        }
        return context


def hide_lobby_message(
    message,
    is_automated=False,
    moderator=None,
    reason="",
    log_action=True,
):
    """Hide a single lobby message and log the action."""
    lobby = get_lobby()
    if message.room_id != lobby.id:
        raise ValueError("hide_lobby_message only accepts persisted Lobby messages")
    message.hidden = True
    message.save(update_fields=["hidden"])
    get_first_msg_id.dirty(lobby.id)
    transaction.on_commit(lambda: Room.dirty_cache(lobby.id))
    if log_action:
        ChatModerationLog.log_action(
            message=message,
            action="hide",
            reason=reason,
            is_automated=is_automated,
            moderator=moderator,
        )


def notify_chat_mute(profile, mute_until=None, reason="", moderator=None):
    if mute_until:
        until = timezone.localtime(mute_until).strftime("%Y-%m-%d %H:%M")
        summary = _("Your chat access has been muted until %(until)s.") % {
            "until": until
        }
    else:
        until = ""
        summary = _("Your chat access has been muted permanently.")

    if reason:
        reason_text = _("Reason: %(reason)s") % {"reason": reason}
        html_link = format_html("{}<br>{}", summary, reason_text)
    else:
        html_link = summary

    Notification.objects.create_notification(
        owner=profile,
        category=NotificationCategory.CHAT_MUTE,
        html_link=html_link,
        author=moderator,
        extra_data={
            "type": "chat_mute_notice",
            "mute_until": until,
            "reason": reason,
        },
        deduplicate=False,
    )
    event.post(
        encrypt_channel("chat_" + str(profile.id)),
        {
            "type": "chat_muted",
            "mute_until": mute_until.isoformat() if mute_until else None,
        },
    )


def mute_chat_user(
    message,
    is_automated=False,
    moderator=None,
    reason="",
    mute_type="permanent",
    hide_room_messages=False,
    log_action=True,
):
    """Suspend a user from all chat, with optional current-channel cleanup."""
    now = timezone.now()
    mute_until = None
    duration_days = None
    action = "mute_perm"

    if mute_type == "temporary":
        duration_days = get_temporary_mute_duration_days(message.author)
        base_time = message.author.mute_until or now
        if base_time < now:
            base_time = now
        mute_until = base_time + timezone.timedelta(days=duration_days)
        action = "mute_temp"

    message.author.mute = True
    message.author.mute_until = mute_until
    message.author.mute_reason = reason
    message.author.save(update_fields=["mute", "mute_until", "mute_reason"])
    Profile.dirty_cache(message.author_id)
    hidden_message_count = 0
    if hide_room_messages:
        last_message_id = 0
        while True:
            message_ids = list(
                Message.objects.filter(
                    room=message.room,
                    author=message.author,
                    hidden=False,
                    id__gt=last_message_id,
                )
                .order_by("id")
                .values_list("id", flat=True)[:CHAT_MODERATION_BATCH_SIZE]
            )
            if not message_ids:
                break
            hidden_message_count += Message.objects.filter(id__in=message_ids).update(
                hidden=True
            )
            last_message_id = message_ids[-1]
        replacement = (
            Message.objects.filter(
                room=message.room,
                hidden=False,
                kind=Message.Kind.USER,
            )
            .order_by("-id")
            .values("id", "time")
            .first()
        )
        Room.objects.filter(id=message.room_id).update(
            last_msg_id=replacement["id"] if replacement else None,
            last_activity_at=replacement["time"] if replacement else None,
        )
        get_first_msg_id.dirty(message.room_id)
        transaction.on_commit(lambda: Room.dirty_cache(message.room_id))
        transaction.on_commit(
            lambda: broadcast_room_event(
                message.room_id,
                {
                    "type": "user_messages_hidden",
                    "room": message.room_id,
                    "user": message.author_id,
                },
            )
        )
    if log_action:
        ChatModerationLog.log_action(
            message=message,
            action=action,
            reason=reason,
            is_automated=is_automated,
            moderator=moderator,
            mute_until=mute_until,
            mute_duration_days=duration_days,
        )
        if hidden_message_count:
            ChatModerationLog.log_action(
                message=message,
                action="hide",
                reason=reason,
                is_automated=is_automated,
                moderator=moderator,
            )
    notify_chat_mute(
        message.author,
        mute_until=mute_until,
        reason=reason,
        moderator=moderator,
    )
    return {
        "action": action,
        "mute_until": mute_until,
        "mute_duration_days": duration_days,
        "hidden_message_count": hidden_message_count,
    }


@transaction.atomic
def site_mute_chat_user(
    message,
    user,
    profile,
    *,
    reason="",
    mute_type="permanent",
    hide_room_messages=False,
    log_action=True,
):
    """Apply a site mute from a channel the site administrator belongs to."""
    room = Room.objects.select_for_update().get(pk=message.room_id)
    message = (
        Message.objects.select_for_update().select_related("author").get(pk=message.pk)
    )
    memberships = {
        membership.user_id: membership
        for membership in UserRoom.objects.select_for_update()
        .filter(
            room=room,
            user_id__in=[profile.id, message.author_id],
        )
        .order_by("pk")
    }
    policy = RoomPolicy(user, profile, room, memberships.get(profile.id))
    if (
        room.room_type != Room.Type.CHANNEL
        or not policy.is_superuser_override
        or not policy.can_moderate_target(memberships.get(message.author_id))
    ):
        raise RoomPermissionDenied(_("You cannot mute this member."))

    with revisions.create_revision():
        revisions.set_comment(
            _("Site-wide mute: %(message)s") % {"message": message.body}
        )
        revisions.set_user(user)
        return mute_chat_user(
            message,
            moderator=profile,
            reason=reason,
            mute_type=mute_type,
            hide_room_messages=hide_room_messages,
            log_action=log_action,
        )


def delete_message(request):
    ret = {"delete": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    if not request.user.is_authenticated:
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.get(id=messid)
    except:
        return HttpResponseBadRequest()

    try:
        hide_room_message(mess, request.user, request.profile)
    except Exception as error:
        if hasattr(error, "status"):
            return JsonResponse(
                {"error": error.message, "code": error.code}, status=error.status
            )
        return HttpResponseBadRequest()
    get_first_msg_id.dirty(mess.room_id)
    return JsonResponse(ret)


def mute_message(request):
    ret = {"mute": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    if not request.user.is_authenticated:
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.select_related("room", "author").get(id=messid)
    except:
        return HttpResponseBadRequest()

    if not mess.author_id or mess.author_id == request.profile.id:
        return HttpResponseBadRequest()

    mute_type = request.POST.get("mute_type", "permanent")
    scope = request.POST.get("scope")
    reason = request.POST.get("reason", "").strip()
    hide_room_messages = request.POST.get("hide_room_messages") == "1"

    if mute_type not in ("temporary", "permanent"):
        return HttpResponseBadRequest()
    if scope is None:
        scope = "site" if mute_type == "permanent" else "room"

    if scope == "site":
        try:
            site_mute_chat_user(
                mess,
                request.user,
                request.profile,
                reason=reason,
                mute_type=mute_type,
                hide_room_messages=hide_room_messages,
            )
        except RoomError as error:
            return JsonResponse(
                {"error": error.message, "code": error.code},
                status=error.status,
            )
    elif scope == "room" and mute_type == "temporary" and not hide_room_messages:
        try:
            mute_member(
                mess.room,
                request.user,
                request.profile,
                mess.author,
                reason,
            )
        except Exception as error:
            if hasattr(error, "status"):
                return JsonResponse(
                    {"error": error.message, "code": error.code},
                    status=error.status,
                )
            return HttpResponseBadRequest()
    else:
        return HttpResponseBadRequest()

    return JsonResponse(ret)


def check_valid_message(request, room, membership):
    if request.in_contest and request.participation.contest.use_clarifications:
        raise RoomPermissionDenied(_("Chat is disabled during this contest."))

    body = request.POST["body"].strip()
    if room.channel_kind == Room.ChannelKind.LOBBY and len(body) > 200:
        raise RoomError(
            _("Lobby messages may contain at most 200 characters."),
            code="message_too_long",
        )

    policy = RoomPolicy(request.user, request.profile, room, membership)
    if not policy.can_post():
        raise RoomPermissionDenied(_("You cannot post in this room."))
    if is_chat_muted(request.profile):
        raise RoomPermissionDenied(_("You are muted from chat."), code="chat_muted")
    if active_room_mute(room, request.profile, timezone.now()):
        raise RoomPermissionDenied(
            _("You are muted in this room."),
            code="room_muted",
        )
    if not can_use_community_features(request.user, request.profile):
        raise RoomPermissionDenied(
            _("Solve a problem before using chat."),
            code="community_access_required",
        )

    last_msg = (
        Message.objects.filter(room=room, kind=Message.Kind.USER)
        .values("author_id", "body")
        .first()
    )
    if (
        room.room_type in (Room.Type.DIRECT, Room.Type.GROUP)
        and last_msg
        and last_msg["author_id"] == request.profile.id
        and last_msg["body"] == body
    ):
        raise RoomError(
            _("Consecutive duplicate messages are not allowed."),
            code="duplicate_message",
        )

    now = timezone.now()
    if room.channel_kind == Room.ChannelKind.LOBBY:
        four_last_msg = list(
            Message.objects.filter(room=room, kind=Message.Kind.USER)
            .order_by("-id")
            .values_list("author_id", "time")[:4]
        )
        if len(four_last_msg) >= 4:
            same_author = all(
                author_id == request.profile.id for author_id, _ in four_last_msg
            )
            time_diff = now - four_last_msg[3][1]
            if same_author and time_diff.total_seconds() < 300:
                retry_after = max(1, math.ceil(300 - time_diff.total_seconds()))
                error = RoomError(
                    _("Please wait %(seconds)s seconds before posting again.")
                    % {"seconds": retry_after},
                    code="message_rate_limited",
                    status=429,
                )
                error.retry_after = retry_after
                raise error
    elif room.room_type == Room.Type.CHANNEL:
        window_seconds = getattr(settings, "CHAT_CHANNEL_MESSAGE_WINDOW_SECONDS", 10)
        message_limit = getattr(settings, "CHAT_CHANNEL_MESSAGE_LIMIT", 10)
        recent_times = list(
            Message.objects.filter(
                room=room,
                author=request.profile,
                kind=Message.Kind.USER,
                time__gte=now - timezone.timedelta(seconds=window_seconds),
            )
            .order_by("-time")
            .values_list("time", flat=True)[:message_limit]
        )
        if len(recent_times) >= message_limit:
            elapsed = (now - recent_times[-1]).total_seconds()
            retry_after = max(1, math.ceil(window_seconds - elapsed))
            error = RoomError(
                _("Please wait %(seconds)s seconds before posting again.")
                % {"seconds": retry_after},
                code="message_rate_limited",
                status=429,
            )
            error.retry_after = retry_after
            raise error

    return True


@login_required
def post_message(request):
    ret = {"msg": "posted"}

    if request.method != "POST":
        return HttpResponseBadRequest()
    body = request.POST.get("body", "")
    if len(body) > 5000 or not body.strip():
        return JsonResponse(
            {
                "error": _("Messages must contain between 1 and 5,000 characters."),
                "code": "invalid_message_length",
            },
            status=400,
        )

    try:
        room = (
            Room.objects.get(id=request.POST["room"])
            if request.POST.get("room")
            else get_lobby()
        )
    except Room.DoesNotExist:
        return HttpResponseBadRequest()

    try:
        with transaction.atomic():
            # Serialize validation and insertion per sender/room. This makes the
            # duplicate and flood rules authoritative under concurrent requests,
            # and orders posting against membership removal and room mutes.
            membership = (
                UserRoom.objects.select_for_update()
                .filter(room=room, user=request.profile)
                .first()
            )
            check_valid_message(request, room, membership)

            reply_to = None
            reply_to_raw = request.POST.get("reply_to")
            if reply_to_raw:
                try:
                    candidate = Message.objects.filter(hidden=False).get(
                        id=int(reply_to_raw)
                    )
                    # Only link a parent from the same persisted room. A cross-room
                    # or vanished/hidden parent is dropped so the post still lands.
                    if (
                        candidate.room_id == room.id
                        and candidate.kind == Message.Kind.USER
                    ):
                        reply_to = candidate
                except (ValueError, Message.DoesNotExist):
                    reply_to = None

            new_message = Message.objects.create(
                author=request.profile,
                body=request.POST["body"],
                room=room,
                reply_to=reply_to,
            )
            Room.objects.filter(pk=room.id).filter(
                Q(last_msg_id__isnull=True) | Q(last_msg_id__lt=new_message.id)
            ).update(
                last_msg_id=new_message.id,
                last_activity_at=new_message.time,
            )
            UserRoom.objects.filter(pk=membership.pk).update(
                last_read_message_id=new_message.id,
                last_seen=timezone.now(),
                unread_count=0,
            )
            transaction.on_commit(
                lambda: broadcast_room_event(
                    room.id,
                    {
                        "type": "message",
                        "author_id": request.profile.id,
                        "message": new_message.id,
                        "room": room.id,
                        "tmp_id": request.POST.get("tmp_id"),
                    },
                )
            )
    except RoomError as error:
        payload = {"error": error.message, "code": error.code}
        if hasattr(error, "retry_after"):
            payload["retry_after"] = error.retry_after
        return JsonResponse(payload, status=error.status)
    Room.dirty_cache(room.id)
    get_first_msg_id.dirty(room.id)

    return JsonResponse(ret)


@login_required
@transaction.atomic
def react_message(request):
    """Add / change / remove the requesting user's single reaction on a message.

    Messenger-style: at most one reaction per user per message. Sending the same
    code again removes it (toggle off); a different code replaces the old one.
    Returns the fresh reaction summary for the message.
    """
    if request.method != "POST":
        return HttpResponseBadRequest()

    try:
        message = (
            Message.objects.filter(hidden=False)
            .select_related("room")
            .get(id=int(request.POST["message"]))
        )
    except (KeyError, ValueError, Message.DoesNotExist):
        return HttpResponseBadRequest()

    reaction = request.POST.get("reaction")
    if reaction not in CHAT_REACTION_CODES or message.kind != Message.Kind.USER:
        return HttpResponseBadRequest()

    room = message.room
    membership = (
        UserRoom.objects.select_for_update()
        .filter(room=room, user=request.profile)
        .first()
    )
    if not RoomPolicy(request.user, request.profile, room, membership).can_react():
        return HttpResponseForbidden()

    # A muted user is silenced from chat interaction, reactions included.
    if (
        is_chat_muted(request.profile)
        or not can_use_community_features(request.user, request.profile)
        or active_room_mute(room, request.profile, timezone.now())
    ):
        return HttpResponseForbidden()

    profile = request.profile
    existing = (
        MessageReaction.objects.select_for_update()
        .filter(message=message, user=profile)
        .first()
    )
    if existing is None:
        try:
            # Savepoint so a lost insert race doesn't poison an outer transaction.
            with transaction.atomic():
                MessageReaction.objects.create(
                    message=message, user=profile, reaction=reaction
                )
        except IntegrityError:
            # Lost a race with a concurrent request from the same user -> update.
            MessageReaction.objects.filter(message=message, user=profile).update(
                reaction=reaction
            )
        my_reaction = reaction
    elif existing.reaction == reaction:
        existing.delete()
        my_reaction = None  # toggled off
    else:
        existing.reaction = reaction
        existing.save(update_fields=["reaction", "created"])
        my_reaction = reaction

    # We already know the viewer's resulting reaction, so skip re-querying it.
    summary = get_reactions_summary([message.id], profile, include_my_reaction=False)[
        message.id
    ]
    summary["my_reaction"] = my_reaction
    transaction.on_commit(lambda: broadcast_reaction(request, message, room, summary))
    return JsonResponse(summary)


def broadcast_reaction(request, message, room, summary):
    """Push a reaction update over the event daemon.

    One room-scoped event replaces per-member fanout for every room type.
    """
    payload = {
        "type": "reaction",
        "message": message.id,
        "counts": summary["counts"],
        "total": summary["total"],
        "user_id": request.profile.id,
        # The reactor's resulting reaction (code, or None if toggled off) so their
        # OTHER tabs/devices can update their own highlight without a reload.
        # Already public via counts/the who-reacted list, so no new info is leaked.
        "actor_reaction": summary.get("my_reaction"),
    }
    payload["room"] = room.id
    broadcast_room_event(room.id, payload)


def reaction_render_context(messages, profile):
    """Context vars needed to render reaction pills/pickers for a set of messages.

    Bundles the batched per-message summary with the (constant) emoji mappings so
    every message-render path can drop them in with a single ``**`` spread.
    """
    return {
        "reactions": get_reactions_summary([m.id for m in messages], profile),
        "chat_reactions": CHAT_REACTIONS,
        "chat_reaction_emoji": CHAT_REACTION_EMOJI,
        "chat_reaction_labels": CHAT_REACTION_LABELS,
        "chat_reaction_image_urls": get_reaction_image_urls(),
    }


def message_permission_context(messages, user, profile, room, membership=None):
    if membership is None:
        membership = get_membership(room, profile)
    policy = RoomPolicy(user, profile, room, membership)
    author_ids = {message.author_id for message in messages if message.author_id}
    memberships = {
        row.user_id: row
        for row in UserRoom.objects.filter(
            room=room,
            user_id__in=author_ids,
            state=UserRoom.State.ACTIVE,
        )
    }
    prior_room_mutes = {}
    if policy.is_admin or policy.is_moderator or policy.is_superuser_override:
        prior_room_mutes = dict(
            RoomMute.objects.filter(room=room, target_id__in=author_ids)
            .values("target_id")
            .annotate(total=Count("id"))
            .values_list("target_id", "total")
        )
    prior_site_mutes = {}
    if user.is_superuser and room.room_type == Room.Type.CHANNEL:
        prior_site_mutes = dict(
            ChatModerationLog.objects.filter(
                message__author_id__in=author_ids,
                action="mute_temp",
            )
            .values("message__author_id")
            .annotate(total=Count("id"))
            .values_list("message__author_id", "total")
        )
    return {
        "can_hide_messages": {
            message.id: policy.can_hide_message(
                message, memberships.get(message.author_id)
            )
            for message in messages
        },
        "can_mute_messages": {
            message.id: policy.can_moderate_target(memberships.get(message.author_id))
            for message in messages
        },
        "room_mute_duration_days": {
            message.id: min(
                prior_room_mutes.get(message.author_id, 0) + 1,
                ROOM_MUTE_MAX_DAYS,
            )
            for message in messages
            if message.author_id
        },
        "site_mute_duration_days": {
            message.id: min(
                prior_site_mutes.get(message.author_id, 0) + 1,
                CHAT_TEMP_MUTE_CAP_DAYS,
            )
            for message in messages
            if message.author_id
        },
    }


def get_reaction_image_urls():
    """code -> static URL for reactions rendered as an image instead of an emoji."""
    return {code: static(path) for code, path in CHAT_REACTION_IMAGES.items()}


REPLY_SNIPPET_LIMIT = 60
# A body that is ONLY a markdown image (optionally surrounded by whitespace).
_IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")


def build_reply_snippet(body):
    """Plain-text ~60-char preview of a parent message for the reply quote.

    Text only (never markdown-rendered) so the quote can't inject HTML. An
    image-only body has no text to show, so it collapses to an "[image]" marker.
    """
    body = body or ""
    if _IMAGE_ONLY_RE.match(body):
        return "[image]"
    text = " ".join(body.split())
    if len(text) > REPLY_SNIPPET_LIMIT:
        return text[:REPLY_SNIPPET_LIMIT] + "…"
    return text


def get_reply_quotes(messages, viewer=None):
    """Map child message id -> quote data for its parent (single level, no N+1).

    Only messages that reply appear in the result. All parents are fetched in ONE
    query; a missing or hidden parent is marked unavailable. Warms the Profile and
    public-identity caches in bulk so get_public_username() below stays 0-query,
    while still masking authors who hid their identity ("Disabled user").
    """
    parent_ids = {m.reply_to_id for m in messages if m.reply_to_id}
    if not parent_ids:
        return {}

    parents = {
        row["id"]: row
        for row in Message.objects.filter(id__in=parent_ids).values(
            "id", "author_id", "body", "hidden", "room_id"
        )
    }

    # Bulk-warm both caches (constant number of queries, not per-author) so the
    # get_public_username() calls in the loop never hit the DB.
    author_ids = {
        p["author_id"] for p in parents.values() if not p["hidden"] and p["author_id"]
    }
    if author_ids:
        Profile.get_cached_instances(*author_ids)
        get_profile_public_identity.batch([(aid,) for aid in author_ids])

    quotes = {}
    for message in messages:
        pid = message.reply_to_id
        if not pid:
            continue
        parent = parents.get(pid)
        # Re-check same-room at render time: never trust the stored FK to point
        # at a parent in this message's room (post_message enforces it on write,
        # but rendering must not leak a cross-room parent if that ever slips).
        if parent is None or parent["hidden"] or parent["room_id"] != message.room_id:
            quotes[message.id] = {"unavailable": True}
        else:
            quotes[message.id] = {
                "parent_id": pid,
                "author_id": parent["author_id"],
                "author_name": (
                    Profile(id=parent["author_id"]).get_public_username(viewer)
                    if parent["author_id"]
                    else _("Deleted user")
                ),
                "snippet": build_reply_snippet(parent["body"]),
                "unavailable": False,
            }
    return quotes


def reply_render_context(messages, viewer=None):
    """Context vars for rendering reply quotes for a set of messages."""
    return {"reply_quotes": get_reply_quotes(messages, viewer)}


@login_required
def reaction_list(request):
    """Render the capped list of users who reacted to a message.

    Public lobby messages can receive many reactions, so this groups by reaction
    type and shows only the first few users per type. Batches the Profile lookup
    so link_user/gravatar in the template read from cache instead of hitting the
    DB once per displayed reactor.
    """
    if request.method != "GET":
        return HttpResponseBadRequest()

    try:
        message = (
            Message.objects.filter(hidden=False)
            .select_related("room")
            .get(id=int(request.GET["message"]))
        )
    except (KeyError, ValueError, Message.DoesNotExist):
        return HttpResponseBadRequest()

    if not can_access_room(request, message.room):
        return HttpResponseForbidden()

    reaction_filter = request.GET.get("reaction") or None
    if reaction_filter is not None and reaction_filter not in CHAT_REACTION_CODES:
        return HttpResponseBadRequest()

    reaction_qs = MessageReaction.objects.filter(
        message=message, reaction__in=CHAT_REACTION_CODES
    )
    if reaction_filter is not None:
        reaction_qs = reaction_qs.filter(reaction=reaction_filter)
    counts = dict(
        reaction_qs.values("reaction")
        .annotate(total=Count("id"))
        .values_list("reaction", "total")
    )
    visible_reactions = (
        [(reaction_filter, CHAT_REACTION_EMOJI[reaction_filter])]
        if reaction_filter is not None
        else CHAT_REACTIONS
    )
    reaction_sections = []
    displayed_user_ids = []
    for code, emoji in visible_reactions:
        total = counts.get(code, 0)
        if not total:
            continue
        user_ids = list(
            MessageReaction.objects.filter(message=message, reaction=code)
            .order_by("id")
            .values_list("user_id", flat=True)[:REACTION_LIST_PER_TYPE_LIMIT]
        )
        displayed_user_ids.extend(user_ids)
        reaction_sections.append(
            {
                "code": code,
                "emoji": emoji,
                "label": CHAT_REACTION_LABELS[code],
                "total": total,
                "displayed_count": len(user_ids),
                "has_more": total > len(user_ids),
                "users": user_ids,
            }
        )

    if displayed_user_ids:
        # Warm the Profile cache once for all displayed reactors.
        Profile.get_cached_instances(*displayed_user_ids)

    return render(
        request,
        "chat/reaction_list.html",
        {
            "reaction_sections": reaction_sections,
            "chat_reaction_image_urls": get_reaction_image_urls(),
        },
    )


def can_access_room(request, room):
    if room is None:
        return False
    membership = get_membership(room, request.profile)
    return RoomPolicy(request.user, request.profile, room, membership).can_view()


@login_required
def chat_message_ajax(request):
    if request.method != "GET":
        return HttpResponseBadRequest()

    try:
        message = Message.objects.filter(hidden=False).get(
            id=int(request.GET["message"])
        )
        room = message.room
        if not can_access_room(request, room):
            return HttpResponse("Unauthorized", status=401)
    except (KeyError, ValueError, Message.DoesNotExist):
        return HttpResponseBadRequest()
    return render(
        request,
        "chat/message.html",
        {
            "message": message,
            "can_chat": can_use_community_features(request.user, request.profile),
            "can_interact_room": (
                RoomPolicy(
                    request.user,
                    request.profile,
                    room,
                    get_membership(room, request.profile),
                ).can_post()
                and not is_chat_muted(request.profile)
                and not active_room_mute(room, request.profile, timezone.now())
                and can_use_community_features(request.user, request.profile)
            ),
            **message_permission_context(
                [message], request.user, request.profile, room
            ),
            **reaction_render_context([message], request.profile),
            **reply_render_context([message], request.user),
        },
    )


@login_required
def update_last_seen(request, **kwargs):
    if "room_id" in kwargs:
        room_id = kwargs["room_id"]
    elif request.method == "GET":
        room_id = request.GET.get("room")
    elif request.method == "POST":
        room_id = request.POST.get("room")
    else:
        return HttpResponseBadRequest()
    try:
        profile = request.profile
        room = Room.objects.filter(id=int(room_id)).first() if room_id else get_lobby()
    except (Room.DoesNotExist, TypeError, ValueError):
        return HttpResponseBadRequest()

    if room is None or not can_access_room(request, room):
        return HttpResponseBadRequest()

    mark_room_read(room, profile)

    return JsonResponse({"msg": "updated"})


@cache_wrapper(prefix="cgoc", timeout=120)
def get_online_count():
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    return Profile.objects.filter(last_access__gte=last_5_minutes).count()


def get_user_online_status(profile):
    time_diff = timezone.now() - profile.get_last_access()
    is_online = time_diff <= timezone.timedelta(minutes=5)
    return is_online


def user_online_status_ajax(request):
    if request.method != "GET":
        return HttpResponseBadRequest()

    user_id = request.GET.get("user")

    if user_id:
        try:
            user_id = int(user_id)
            user = Profile.objects.get(id=user_id)
        except Exception:
            return HttpResponseBadRequest()

        is_online = get_user_online_status(user)
        is_self_room = user.id == request.profile.id
        return render(
            request,
            "chat/user_online_status.html",
            {
                "other_user": user,
                "other_online": is_online,
                "is_ignored": (
                    False if is_self_room else Ignore.is_ignored(request.profile, user)
                ),
                "is_self_room": is_self_room,
            },
        )
    else:
        return render(
            request,
            "chat/user_online_status.html",
            {
                "online_count": get_online_count(),
            },
        )


def get_status_context(profile, include_ignored=False, section=None):
    ignored_room_ids = (
        set() if include_ignored else Ignore.get_ignored_room_ids(profile)
    )
    if section not in ROOM_LIST_SECTIONS:
        section = None
    memberships, has_more = get_room_page(
        profile,
        exclude_room_ids=ignored_room_ids,
        section=section,
    )
    sections = [
        {
            "key": section or "all",
            "title": _("Recent"),
            "memberships": memberships,
            "has_more": has_more,
            "next_cursor": encode_room_list_cursor(memberships, has_more),
            "room_list": [],
        }
    ]
    Room.prefetch_room_cache([row.room_id for row in memberships])
    unread_counts = unread_counts_for_memberships(memberships)
    ignored_users = set() if include_ignored else get_ignored_user_ids(profile)
    other_ids = {
        membership.room.other_user_id(profile)
        for membership in memberships
        if membership.room.room_type == Room.Type.DIRECT
    }
    other_ids.discard(None)
    Profile.get_cached_instances(*other_ids)
    Profile.prefetch_cache_last_access(*other_ids)

    for section in sections:
        for membership in section.pop("memberships"):
            room = membership.room
            row = {
                "room": room.id,
                "room_type": room.room_type,
                "channel_kind": room.channel_kind,
                "name": room.name,
                "avatar_url": room.get_avatar_url(),
                "last_msg": room.get_last_message(),
                "unread_count": unread_counts.get(room.id, 0),
            }
            if room.room_type == Room.Type.DIRECT:
                other_id = room.other_user_id(profile)
                if other_id in ignored_users:
                    continue
                if other_id:
                    other = Profile(id=other_id)
                    row.update(
                        {
                            "user": other,
                            "name": (
                                _("Saved Messages")
                                if other_id == profile.id
                                else other.get_public_username(profile.user)
                            ),
                            "is_self": other_id == profile.id,
                            "is_online": get_user_online_status(other),
                        }
                    )
            section["room_list"].append(row)
    return sections


@login_required
def online_status_ajax(request):
    lobby = get_lobby()
    lobby_membership = get_membership(lobby, request.profile)
    section = request.GET.get("section") or None
    return render(
        request,
        "chat/online_status.html",
        {
            "status_sections": get_status_context(request.profile, section=section),
            "unread_count_lobby": get_unread_count(lobby, request.profile),
            "lobby_room": lobby,
            "lobby_hidden": lobby_membership.is_hidden if lobby_membership else False,
        },
    )


@login_required
def get_or_create_room(request):
    if request.method == "GET":
        decrypted_other_id = request.GET.get("other")
    elif request.method == "POST":
        decrypted_other_id = request.POST.get("other")
    else:
        return HttpResponseBadRequest()

    request_id, other_id = decrypt_url(decrypted_other_id)
    if not other_id or not request_id or request_id != request.profile.id:
        return HttpResponseBadRequest()

    try:
        other_user = Profile.objects.get(id=int(other_id))
    except Exception:
        return HttpResponseBadRequest()

    user = request.profile

    if not other_user or not user:
        return HttpResponseBadRequest()

    room = Room.get_or_create_room(other_user, user)

    room_url = reverse("chat", kwargs={"room_id": room.id})
    if request.method == "GET":
        return JsonResponse(
            {
                "room": room.id,
                "other_user_id": other_user.id,
                "url": room_url,
            }
        )
    return HttpResponseRedirect(room_url)


def get_unread_count(rooms, user):
    if isinstance(rooms, Room):
        membership = UserRoom.objects.filter(
            user=user, room=rooms, state=UserRoom.State.ACTIVE
        ).first()
        if not membership or membership.is_hidden:
            return 0
        return unread_counts_for_memberships([membership]).get(rooms.id, 0)
    memberships = list(
        UserRoom.objects.filter(
            user=user,
            room_id__in=rooms,
            state=UserRoom.State.ACTIVE,
            is_hidden=False,
        )
    )
    counts = unread_counts_for_memberships(memberships)
    return [
        {"room": membership.room_id, "unread_count": counts[membership.room_id]}
        for membership in memberships
        if membership.room_id in counts
    ]


@login_required
@require_POST
def toggle_ignore(request, **kwargs):
    try:
        user_id = int(kwargs["user_id"])
    except (KeyError, TypeError, ValueError):
        return HttpResponseBadRequest()

    # The Ignore many-to-many manager requires a database-bound Profile rather
    # than the lightweight instances returned by get_cached_instances().
    try:
        other_user = Profile.objects.only("id").get(id=user_id)
    except Profile.DoesNotExist:
        return HttpResponseBadRequest()

    if other_user.id == request.profile.id:
        return HttpResponseBadRequest()

    ignored = Ignore.toggle_ignore(request.profile, other_user)
    fallback_url = reverse("chat", args=[""])
    next_url = request.POST.get("next") or fallback_url
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = fallback_url
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ignored": ignored, "redirect": next_url})
    return HttpResponseRedirect(next_url)
