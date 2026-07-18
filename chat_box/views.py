from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.views.generic import ListView

from reversion import revisions

from judge import event_poster as event
from judge.caching import cache_wrapper
from judge.models.notification import Notification, NotificationCategory
from chat_box.models import (
    ChatModerationLog,
    Ignore,
    Message,
    MessageReaction,
    Profile,
    Room,
    UserRoom,
    CHAT_REACTIONS,
    CHAT_REACTION_CODES,
    CHAT_REACTION_EMOJI,
    CHAT_REACTION_IMAGES,
    CHAT_REACTION_LABELS,
    get_first_msg_id,
    get_ignored_user_ids,
    get_user_room_list,
)
from chat_box.utils import (
    encrypt_url,
    decrypt_url,
    encrypt_channel,
    get_reactions_summary,
    get_unread_boxes,
)

CHAT_TEMP_MUTE_CAP_DAYS = 30
REACTION_LIST_PER_TYPE_LIMIT = 10


def can_mute_chat_temporarily(user):
    return user.has_perm("judge.change_comment")


def can_mute_chat_permanently(user):
    return user.is_superuser


def can_mute_chat(user):
    return can_mute_chat_temporarily(user) or can_mute_chat_permanently(user)


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
        page_size = self.follow_up_page_size
        try:
            last_id = int(request.GET.get("last_id"))
        except Exception:
            last_id = 2**63 - 1
            page_size = self.first_page_size
        only_messages = request.GET.get("only_messages")

        if request_room:
            try:
                self.room = Room.objects.get(id=request_room)
                if not can_access_room(request, self.room):
                    return HttpResponseBadRequest()
            except Room.DoesNotExist:
                return HttpResponseBadRequest()
        else:
            request_room = None

        self.room_id = request_room
        self.messages = self.get_message_page(last_id, page_size)
        if not only_messages:
            return super().get(request, *args, **kwargs)

        return render(
            request,
            "chat/message_list.html",
            {
                "object_list": self.messages,
                "has_next": self.has_next(),
                **reaction_render_context(self.messages, request.profile),
            },
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["title"] = self.title
        context["last_msg"] = event.last()
        context["status_sections"] = get_status_context(self.request.profile)
        context["room"] = self.room_id
        context["has_next"] = self.has_next()
        context["unread_count_lobby"] = get_unread_count(None, self.request.profile)
        context["is_chat_muted"] = is_chat_muted(self.request.profile)
        context["can_mute_chat_temporarily"] = can_mute_chat_temporarily(
            self.request.user
        )
        context["can_mute_chat_permanently"] = can_mute_chat_permanently(
            self.request.user
        )
        context["chat_channel"] = encrypt_channel(
            "chat_" + str(self.request.profile.id)
        )
        context["chat_lobby_channel"] = encrypt_channel("chat_lobby")
        context.update(reaction_render_context(self.messages, self.request.profile))
        if self.room:
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
        else:
            context["online_count"] = get_online_count()
        context["message_template"] = {
            "author_id": self.request.profile.id,
            "id": "$id",
            "time": timezone.now(),
            "body": "$body",
        }
        return context


def hide_lobby_message(message, is_automated=False, moderator=None, reason=""):
    """Hide a single lobby message and log the action."""
    message.hidden = True
    message.save(update_fields=["hidden"])
    get_first_msg_id.dirty(None)
    ChatModerationLog.log_action(
        message=message,
        action="hide",
        reason=reason,
        is_automated=is_automated,
        moderator=moderator,
    )


def notify_chat_mute(profile, mute_until=None, reason=""):
    if mute_until:
        until = timezone.localtime(mute_until).strftime("%Y-%m-%d %H:%M")
        summary = _("Your chat access has been muted until %(until)s.") % {
            "until": until
        }
    else:
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
        author=None,
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
):
    """Mute a user, hide lobby messages, log the action, and notify them."""
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
    Message.objects.filter(room=None, author=message.author).update(hidden=True)
    get_first_msg_id.dirty(None)
    ChatModerationLog.log_action(
        message=message,
        action=action,
        reason=reason,
        is_automated=is_automated,
        moderator=moderator,
        mute_until=mute_until,
        mute_duration_days=duration_days,
    )
    notify_chat_mute(message.author, mute_until=mute_until, reason=reason)


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

    if (
        not request.user.has_perm("judge.change_comment")
        and request.profile != mess.author
    ):
        return HttpResponseBadRequest()

    room_id = mess.room_id

    if not room_id and request.user.has_perm("judge.change_comment"):
        # Lobby message deleted by staff — shared helper handles hide + cache + log
        hide_lobby_message(mess, moderator=request.profile)
        return JsonResponse(ret)

    mess.hidden = True
    mess.save()

    get_first_msg_id.dirty(room_id)

    # If deleting the last message, update room's last_msg_id
    if room_id:
        room = Room.objects.get(id=room_id)
        if room.last_msg_id == messid:
            # Find the new last visible message
            new_last_msg = (
                Message.objects.filter(room_id=room_id, hidden=False)
                .order_by("-id")
                .first()
            )
            room.last_msg_id = new_last_msg.id if new_last_msg else None
            room.save(update_fields=["last_msg_id"])

        # Dirty the room cache to update last_message in sidebar
        Room.dirty_cache(room_id)

        # Decrement unread_count for users who haven't seen this message yet
        user_rooms = UserRoom.objects.filter(
            room_id=room_id, last_seen__lt=mess.time, unread_count__gt=0
        ).exclude(user=mess.author)
        for user_room in user_rooms:
            user_room.unread_count = max(0, user_room.unread_count - 1)
            user_room.save(update_fields=["unread_count"])
            get_unread_boxes.dirty(user_room.user)

    return JsonResponse(ret)


def mute_message(request):
    ret = {"mute": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    if not request.user.is_authenticated:
        return HttpResponseBadRequest()

    if not can_mute_chat(request.user):
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.get(id=messid)
    except:
        return HttpResponseBadRequest()

    if mess.room_id or mess.author_id == request.profile.id:
        return HttpResponseBadRequest()

    mute_type = request.POST.get("mute_type", "permanent")
    reason = request.POST.get("reason", "").strip()

    if mute_type == "temporary":
        if not can_mute_chat_temporarily(request.user):
            return HttpResponseBadRequest()
    elif mute_type == "permanent":
        if not can_mute_chat_permanently(request.user):
            return HttpResponseBadRequest()
    else:
        return HttpResponseBadRequest()

    if (
        not reason
        and mute_type == "temporary"
        and not can_mute_chat_permanently(request.user)
    ):
        return JsonResponse({"error": _("Reason is required.")}, status=400)

    with revisions.create_revision():
        revisions.set_comment(_("Mute chat") + ": " + mess.body)
        revisions.set_user(request.user)
        mute_chat_user(
            mess,
            moderator=request.profile,
            reason=reason,
            mute_type=mute_type,
        )

    return JsonResponse(ret)


def check_valid_message(request, room):
    if request.in_contest and request.participation.contest.use_clarifications:
        return False

    if not room and len(request.POST["body"]) > 200:
        return False

    if not can_access_room(request, room) or is_chat_muted(request.profile):
        return False

    last_msg = Message.objects.filter(room=room).first()
    if (
        last_msg
        and last_msg.author == request.profile
        and last_msg.body == request.POST["body"].strip()
    ):
        return False

    if not room:
        four_last_msg = Message.objects.filter(room=room).order_by("-id")[:4]
        if len(four_last_msg) >= 4:
            same_author = all(msg.author == request.profile for msg in four_last_msg)
            time_diff = timezone.now() - four_last_msg[3].time
            if same_author and time_diff.total_seconds() < 300:
                return False

    return True


@login_required
def post_message(request):
    ret = {"msg": "posted"}

    if request.method != "POST":
        return HttpResponseBadRequest()
    if len(request.POST["body"]) > 5000 or len(request.POST["body"].strip()) == 0:
        return HttpResponseBadRequest()

    room = None
    if request.POST["room"]:
        room = Room.objects.get(id=request.POST["room"])

    if not check_valid_message(request, room):
        return HttpResponseBadRequest()

    new_message = Message(author=request.profile, body=request.POST["body"], room=room)
    new_message.save()

    if not room:
        event.post(
            encrypt_channel("chat_lobby"),
            {
                "type": "lobby",
                "author_id": request.profile.id,
                "message": new_message.id,
                "room": "None",
                "tmp_id": request.POST.get("tmp_id"),
            },
        )
        if not get_first_msg_id(None):
            get_first_msg_id.dirty(None)
    else:
        Room.dirty_cache(room.id)
        room.last_msg_id = new_message.id
        room.save()

        # Dirty the user room list cache for all users in the room
        for user in room.get_users():
            get_user_room_list.dirty(user.id)

            event_data = {
                "type": "private",
                "author_id": request.profile.id,
                "message": new_message.id,
                "room": room.id,
                "tmp_id": request.POST.get("tmp_id"),
            }

            if user.id != request.profile.id:
                # Update unread count first, then include in event
                UserRoom.objects.filter(user=user, room=room).update(
                    unread_count=F("unread_count") + 1
                )
                get_unread_boxes.dirty(user)
                # Get the new unread count for this room
                user_room = UserRoom.objects.filter(user=user, room=room).first()
                if user_room:
                    event_data["unread_count"] = user_room.unread_count
                    # Include other user's ID for badge update
                    event_data["other_user_id"] = request.profile.id
            elif len(room.get_user_ids()) == 1:
                event_data["other_user_id"] = request.profile.id

            event.post(encrypt_channel("chat_" + str(user.id)), event_data)

        if not get_first_msg_id(room.id):
            get_first_msg_id.dirty(room.id)

    return JsonResponse(ret)


@login_required
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
    if reaction not in CHAT_REACTION_CODES:
        return HttpResponseBadRequest()

    room = message.room
    if not can_access_room(request, room):
        return HttpResponseForbidden()

    # A muted user is silenced from chat interaction, reactions included.
    if is_chat_muted(request.profile):
        return HttpResponseForbidden()

    profile = request.profile
    existing = MessageReaction.objects.filter(message=message, user=profile).first()
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
    broadcast_reaction(request, message, room, summary)
    return JsonResponse(summary)


def broadcast_reaction(request, message, room, summary):
    """Push a reaction update over the event daemon.

    Mirrors post_message: lobby reactions go to the shared "chat_lobby" channel,
    room reactions fan out to each member's personal channel.
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
    if not room:
        payload["room"] = "None"
        event.post(encrypt_channel("chat_lobby"), payload)
    else:
        payload["room"] = room.id
        # Only ids are needed here; get_user_ids() avoids materializing profiles.
        for user_id in room.get_user_ids():
            event.post(encrypt_channel("chat_" + str(user_id)), payload)


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


def get_reaction_image_urls():
    """code -> static URL for reactions rendered as an image instead of an emoji."""
    return {code: static(path) for code, path in CHAT_REACTION_IMAGES.items()}


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

    reaction_qs = MessageReaction.objects.filter(message=message)
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
    return not room or room.contain(request.profile)


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
            **reaction_render_context([message], request.profile),
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
        room = None
        if room_id:
            room = Room.objects.filter(id=int(room_id)).first()
    except Room.DoesNotExist:
        return HttpResponseBadRequest()

    if not can_access_room(request, room):
        return HttpResponseBadRequest()

    user_room, _ = UserRoom.objects.get_or_create(user=profile, room=room)
    user_room.last_seen = timezone.now()
    user_room.unread_count = 0
    user_room.save()

    get_unread_boxes.dirty(profile)

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


def get_online_status(profile, other_profile_ids, rooms=None):
    if not other_profile_ids:
        return None
    other_profiles = Profile.get_cached_instances(*other_profile_ids)
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    ret = []
    if rooms:
        unread_count = get_unread_count(rooms, profile)
        count = {}
        last_msg = {}
        room_of_user = {}

        # Prefetch room info for all rooms
        Room.prefetch_room_cache(rooms)

        for i in unread_count:
            room_id = i["room"]
            room = Room(id=room_id)
            other_id = room.other_user_id(profile)
            if other_id:
                count[other_id] = i["unread_count"]

        for room_id in rooms:
            room = Room(id=room_id)
            other_id = room.other_user_id(profile)
            if other_id:
                last_msg[other_id] = room.get_last_message()
                room_of_user[other_id] = room_id

    for other_profile in other_profiles:
        is_online = False
        if other_profile.get_last_access() >= last_5_minutes:
            is_online = True
        user_dict = {"user": other_profile, "is_online": is_online}
        if rooms:
            user_dict.update(
                {
                    "unread_count": count.get(other_profile.id),
                    "last_msg": last_msg.get(other_profile.id),
                    "room": room_of_user.get(other_profile.id),
                }
            )
        user_dict["url"] = encrypt_url(profile.id, other_profile.id)
        user_dict["is_self"] = profile.id == other_profile.id
        ret.append(user_dict)
    return ret


def get_status_context(profile, include_ignored=False):
    if include_ignored:
        ignored_users = []
    else:
        ignored_users = get_ignored_user_ids(profile)

    # Get user's room list sorted by last_msg_time
    user_rooms = get_user_room_list(profile.id)[:20]

    # Prefetch room info for all rooms
    Room.prefetch_room_cache(user_rooms)

    # Get other users from rooms
    recent_profile_ids = []
    recent_rooms = []

    for room_id in user_rooms:
        other_user_id = Room(id=room_id).other_user_id(profile)
        if other_user_id and other_user_id not in ignored_users:
            recent_profile_ids.append(other_user_id)
            recent_rooms.append(room_id)

    admin_ids = [
        i
        for i in get_admin_ids()
        if i != profile.id and i not in ignored_users and i not in recent_profile_ids
    ]

    Profile.prefetch_cache_last_access(*(recent_profile_ids + admin_ids))

    return [
        {
            "title": _("Recent"),
            "user_list": get_online_status(profile, recent_profile_ids, recent_rooms),
        },
        {
            "title": _("Admin"),
            "user_list": get_online_status(profile, admin_ids),
        },
    ]


@login_required
def online_status_ajax(request):
    return render(
        request,
        "chat/online_status.html",
        {
            "status_sections": get_status_context(request.profile),
            "unread_count_lobby": get_unread_count(None, request.profile),
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
    if rooms:
        return UserRoom.objects.filter(
            user=user, room__in=rooms, unread_count__gt=0
        ).values("unread_count", "room")
    else:  # lobby
        user_room = UserRoom.objects.filter(user=user, room__isnull=True).first()
        if not user_room:
            return 0
        last_seen = user_room.last_seen
        max_lobby_count = 100
        res = (
            Message.objects.filter(room__isnull=True, time__gte=last_seen)
            .exclude(author=user, hidden=True)[:max_lobby_count]
            .count()
        )

        return res


@login_required
def toggle_ignore(request, **kwargs):
    user_id = kwargs["user_id"]
    if not user_id:
        return HttpResponseBadRequest()
    try:
        other_user = Profile.objects.get(id=user_id)
    except:
        return HttpResponseBadRequest()

    if other_user.id == request.profile.id:
        return HttpResponseBadRequest()

    Ignore.toggle_ignore(request.profile, other_user)
    get_unread_boxes.dirty(request.profile)
    next_url = request.GET.get("next", "/")
    return HttpResponseRedirect(next_url)


@cache_wrapper(prefix="gai", timeout=24 * 60, expected_type=list)
def get_admin_ids():
    return list(
        Profile.objects.filter(display_rank="admin").values_list("id", flat=True)
    )
