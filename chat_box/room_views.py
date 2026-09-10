from datetime import datetime, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import Case, IntegerField, When
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from judge.models import Organization, Profile
from judge.jinja2.gravatar import public_gravatar
from judge.models.profile import get_profile_public_identity
from chat_box.exceptions import RoomError, RoomPermissionDenied
from chat_box.forms import RoomAvatarForm
from chat_box.models import Ignore, Room, RoomBan, RoomMute, UserRoom
from chat_box.policies import RoomPolicy
from chat_box.selectors import (
    ROOM_LIST_CURSOR_SALT,
    ROOM_LIST_SECTIONS,
    encode_room_list_cursor,
    get_membership,
    get_room_page,
    unread_counts_for_memberships,
)
from chat_box.services import (
    archive_room,
    ban_member,
    bulk_add_room_members,
    change_room_avatar,
    create_custom_channel,
    create_group,
    create_organization_channel,
    direct_add_member,
    get_invitation_token,
    join_from_invitation,
    leave_room,
    mute_member,
    remove_member,
    rename_room,
    rejoin_organization_channel,
    resolve_invitation,
    restore_room,
    revoke_invitation,
    revoke_room_mute,
    rotate_invitation,
    set_member_role,
    unban_member,
)
from chat_box.services.lobby_sync import set_lobby_moderator
from chat_box.services.events import (
    authorized_event_room_ids,
    broadcast_personal_event,
    chat_event_channels,
    revoke_room_subscriptions,
)
from chat_box.utils import create_chat_event_grant

ARCHIVE_REASON_MAX_LENGTH = Room._meta.get_field("archive_reason").max_length


def room_api(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except RoomError as error:
            return JsonResponse(
                {"error": error.message, "code": error.code},
                status=error.status,
            )
        except (Room.DoesNotExist, Profile.DoesNotExist, UserRoom.DoesNotExist):
            return JsonResponse(
                {"error": _("The requested room or member does not exist.")},
                status=404,
            )

    return wrapped


def _require_post(request):
    if request.method != "POST":
        raise RoomError(_("This action requires a POST request."), status=405)


def _room(room_id):
    return Room.objects.get(pk=room_id)


def _target(request):
    try:
        profile_id = int(request.POST["user_id"])
    except (KeyError, TypeError, ValueError):
        raise RoomError(_("Invalid member."), code="invalid_member")
    return Profile.objects.get(pk=profile_id)


def _selected_member_ids(request):
    values = request.POST.getlist("member_ids") + request.POST.getlist("member_ids[]")
    return list(dict.fromkeys(int(value) for value in values if str(value).isdigit()))


def _room_display(room, viewer):
    if room.room_type == Room.Type.DIRECT:
        other = room.other_user(viewer)
        if other is None:
            return _("Deleted user")
        if other.id == viewer.id:
            return _("Saved Messages")
        return other.get_public_username(viewer.user)
    return room.name


@room_api
def create_group_view(request):
    _require_post(request)
    member_ids = _selected_member_ids(request)
    if member_ids and not request.user.is_superuser:
        raise RoomPermissionDenied(
            _("Only a site administrator may directly add users.")
        )
    if len(member_ids) > 49:
        raise RoomError(
            _(
                "Groups support at most 50 members. Create a channel for a larger community."
            ),
            code="group_full",
        )
    room = create_group(
        request.user,
        request.profile,
        request.POST.get("name"),
        Profile.get_cached_instances(*member_ids),
    )
    return JsonResponse({"room": room.id, "url": reverse("chat", args=[room.id])})


@room_api
def create_channel_view(request):
    _require_post(request)
    channel_kind = request.POST.get("channel_kind")
    if channel_kind == Room.ChannelKind.ORGANIZATION:
        try:
            organization_id = int(request.POST["organization_id"])
            organization = Organization.objects.get(pk=organization_id)
        except (KeyError, TypeError, ValueError, Organization.DoesNotExist):
            raise RoomError(
                _("Select a valid organization."),
                code="invalid_organization",
            )
        room = create_organization_channel(request.user, request.profile, organization)
    elif channel_kind == Room.ChannelKind.CUSTOM:
        member_ids = _selected_member_ids(request)
        if len(member_ids) > 500:
            raise RoomError(
                _("Select at most 500 initial members at a time."),
                code="too_many_initial_members",
            )
        initial_members = Profile.get_cached_instances(*member_ids)
        room = create_custom_channel(
            request.user,
            request.profile,
            request.POST.get("name"),
            initial_members,
        )
    else:
        raise RoomError(_("Invalid channel type."), code="invalid_channel_type")
    return JsonResponse({"room": room.id, "url": reverse("chat", args=[room.id])})


@room_api
def channel_options_view(request):
    if request.method != "GET":
        raise RoomError(_("This action requires a GET request."), status=405)
    organizations = Organization.objects.filter(
        admins=request.profile,
        chat_room__isnull=True,
    )
    term = request.GET.get("term", "").strip()
    if term:
        organizations = organizations.filter(name__icontains=term)
    try:
        page = max(int(request.GET.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    page_size = 20
    start = (page - 1) * page_size
    rows = list(
        organizations.order_by("name", "id").values("id", "name")[
            start : start + page_size + 1
        ]
    )
    return JsonResponse(
        {
            "organizations": rows[:page_size],
            "more": len(rows) > page_size,
            "can_create_custom": request.user.is_superuser,
        }
    )


@login_required
@require_POST
def organization_channel_join_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    room = get_object_or_404(
        Room,
        organization=organization,
        room_type=Room.Type.CHANNEL,
        channel_kind=Room.ChannelKind.ORGANIZATION,
    )
    try:
        rejoin_organization_channel(room, request.profile)
    except RoomError as error:
        messages.error(request, error.message)
        return redirect(
            "organization_home",
            organization.id,
            organization.slug,
        )
    return redirect("chat", room.id)


@room_api
def member_search_view(request):
    if request.method != "GET" or not request.user.is_superuser:
        return HttpResponseForbidden()
    term = request.GET.get("term", "").strip()
    profiles = Profile.objects.filter(user__username__icontains=term).exclude(
        id=request.profile.id
    )
    room_id = request.GET.get("room", "")
    if room_id.isdigit():
        room = _room(int(room_id))
        membership = get_membership(room, request.profile)
        if not RoomPolicy(
            request.user,
            request.profile,
            room,
            membership,
        ).can_view():
            return HttpResponseForbidden()
        if room.channel_kind == Room.ChannelKind.LOBBY:
            ordinary_member_ids = UserRoom.objects.filter(
                room=room,
                state=UserRoom.State.ACTIVE,
                role=UserRoom.Role.MEMBER,
            ).values("user_id")
            profiles = profiles.filter(id__in=ordinary_member_ids)
        else:
            active_member_ids = UserRoom.objects.filter(
                room=room,
                state=UserRoom.State.ACTIVE,
            ).values("user_id")
            profiles = profiles.exclude(id__in=active_member_ids)
    profiles = profiles.values("id", "user__username")[:20]
    return JsonResponse(
        {
            "results": [
                {"id": row["id"], "text": row["user__username"]} for row in profiles
            ]
        }
    )


@room_api
def event_grant_view(request, room_id):
    if request.method != "GET":
        raise RoomError(_("This action requires a GET request."), status=405)
    room = _room(room_id)
    membership = get_membership(room, request.profile)
    if not RoomPolicy(request.user, request.profile, room, membership).can_view():
        return HttpResponseForbidden()
    requested_room_ids = None
    requested = request.GET.get("room_ids", "").strip()
    if requested:
        try:
            requested_room_ids = [int(value) for value in requested.split(",") if value]
        except ValueError:
            raise RoomError(_("Invalid event room list."), code="invalid_room_list")
        if len(requested_room_ids) > 63:
            raise RoomError(_("Too many event rooms requested."), code="too_many_rooms")
    room_ids = authorized_event_room_ids(
        request.profile,
        room.id,
        requested_room_ids,
    )
    channels = chat_event_channels(request.profile.id, room_ids)
    return JsonResponse(
        {
            "grant": create_chat_event_grant(
                request.profile.id,
                room_ids,
                channels,
            ),
            "channels": channels,
        }
    )


@room_api
def room_list_view(request):
    if request.method != "GET":
        raise RoomError(_("This action requires a GET request."), status=405)
    archived = request.GET.get("archived") == "1"
    hidden = request.GET.get("hidden") == "1"
    section = request.GET.get("section") or None
    search = request.GET.get("search", "").strip()[:100]
    if section is not None and section not in ROOM_LIST_SECTIONS:
        raise RoomError(_("Invalid room-list section."), code="invalid_section")
    cursor = None
    cursor_token = request.GET.get("cursor")
    if cursor_token:
        try:
            cursor_data = signing.loads(cursor_token, salt=ROOM_LIST_CURSOR_SALT)
            cursor = (
                datetime.fromisoformat(cursor_data[0]) if cursor_data[0] else None,
                int(cursor_data[1]),
            )
        except (signing.BadSignature, TypeError, ValueError):
            raise RoomError(_("Invalid room-list cursor."), code="invalid_cursor")
    memberships, has_more = get_room_page(
        request.profile,
        cursor=cursor,
        archived=archived,
        hidden=hidden,
        exclude_room_ids=Ignore.get_ignored_room_ids(request.profile),
        section=section,
        search=search,
    )
    unread = unread_counts_for_memberships(memberships)
    rooms = [membership.room for membership in memberships]
    # The JSON payload reads cached previews and avatar URLs below. Warm every
    # room in one batch so a cold cache does not turn a 20-room page into one
    # Room/Message query pair per row.
    Room.prefetch_room_cache([room.id for room in rooms])
    direct_user_ids = set()
    direct_user_by_room = {}
    for room in rooms:
        if room.room_type != Room.Type.DIRECT:
            continue
        other_user_id = room.other_user_id(request.profile)
        if other_user_id:
            direct_user_ids.add(other_user_id)
            direct_user_by_room[room.id] = other_user_id
    if direct_user_ids:
        Profile.get_cached_instances(*direct_user_ids)
        Profile.prefetch_cache_last_access(*direct_user_ids)
        get_profile_public_identity.batch(
            [(profile_id,) for profile_id in direct_user_ids]
        )
    online_cutoff = timezone.now() - timedelta(minutes=5)
    payload = []
    for membership in memberships:
        room = membership.room
        room_payload = {
            "id": room.id,
            "name": _room_display(room, request.profile),
            "room_type": room.room_type,
            "channel_kind": room.channel_kind,
            "unread_count": unread.get(room.id, 0),
            "last_message": room.get_last_message(),
            "last_activity_at": (
                room.last_activity_at.isoformat() if room.last_activity_at else None
            ),
            "hidden": membership.is_hidden,
            "archived": bool(room.archived_at),
            "url": reverse("chat", args=[room.id]),
            "avatar_url": room.get_avatar_url(),
            "actions": RoomPolicy(
                request.user, request.profile, room, membership
            ).room_actions(),
        }
        other_user_id = direct_user_by_room.get(room.id)
        if other_user_id:
            other_profile = Profile(id=other_user_id)
            room_payload.update(
                {
                    "other_user_id": other_user_id,
                    "ignore_url": reverse("toggle_ignore", args=[other_user_id]),
                    "avatar_url": public_gravatar(
                        other_profile,
                        request.user,
                        135,
                    ),
                    "is_online": other_profile.get_last_access() >= online_cutoff,
                    "is_self": other_user_id == request.profile.id,
                }
            )
            if other_user_id != request.profile.id:
                room_payload["actions"]["ignore_url"] = room_payload["ignore_url"]
        payload.append(room_payload)
    next_cursor = encode_room_list_cursor(memberships, has_more)
    return JsonResponse(
        {"rooms": payload, "has_more": has_more, "next_cursor": next_cursor}
    )


@room_api
def room_details_view(request, room_id):
    room = _room(room_id)
    membership = get_membership(room, request.profile)
    policy = RoomPolicy(request.user, request.profile, room, membership)
    if not policy.can_view():
        return HttpResponseForbidden()
    if room.channel_kind == Room.ChannelKind.LOBBY:
        member_queryset = UserRoom.objects.filter(
            room=room,
            state=UserRoom.State.ACTIVE,
            role=UserRoom.Role.MODERATOR,
        )
    else:
        member_queryset = UserRoom.objects.filter(
            room=room, state=UserRoom.State.ACTIVE
        )
    member_search = request.GET.get("member_search", "").strip()
    if member_search:
        member_queryset = member_queryset.filter(
            user__user__username__icontains=member_search
        )
    member_rows = list(
        member_queryset.annotate(
            room_role_order=Case(
                When(role=UserRoom.Role.ADMIN, then=0),
                When(role=UserRoom.Role.MODERATOR, then=1),
                When(role=UserRoom.Role.MEMBER, then=2),
                default=3,
                output_field=IntegerField(),
            )
        )
        .order_by("room_role_order", "user__user__username", "user_id")
        .values("user_id", "role", "manual_role", "synced_role")[:101]
    )
    profiles = {
        profile.id: profile
        for profile in Profile.get_cached_instances(
            *[row["user_id"] for row in member_rows[:100]]
        )
    }
    if profiles:
        get_profile_public_identity.batch([(profile_id,) for profile_id in profiles])
    direct_peer_id = (
        room.other_user_id(request.profile)
        if room.room_type == Room.Type.DIRECT
        else None
    )
    direct_peer = profiles.get(direct_peer_id)
    return JsonResponse(
        {
            "id": room.id,
            "name": _room_display(room, request.profile),
            "room_type": room.room_type,
            "channel_kind": room.channel_kind,
            "archived": bool(room.archived_at),
            "avatar_url": room.get_avatar_url(),
            "has_custom_avatar": bool(room.avatar),
            "role": membership.role if membership else None,
            "member_count": UserRoom.objects.filter(
                room=room, state=UserRoom.State.ACTIVE
            ).count(),
            "members": [
                {
                    "id": row["user_id"],
                    "name": profiles[row["user_id"]].get_public_username(request.user),
                    "url": profiles[row["user_id"]].get_absolute_url(),
                    "css_class": profiles[row["user_id"]].css_class,
                    "role": row["role"],
                    "manual_role": row["manual_role"],
                    "synced_role": row["synced_role"],
                    "avatar_url": public_gravatar(
                        profiles[row["user_id"]], request.user, 80
                    ),
                }
                for row in member_rows[:100]
                if row["user_id"] in profiles
            ],
            "members_truncated": len(member_rows) > 100,
            "ignore_url": (
                reverse("toggle_ignore", args=[direct_peer_id])
                if direct_peer and direct_peer_id != request.profile.id
                else None
            ),
            "ignored": (
                Ignore.is_ignored(request.profile, direct_peer)
                if direct_peer and direct_peer_id != request.profile.id
                else False
            ),
            "permissions": {
                "rename": policy.can_rename(),
                "invite": policy.can_invite(),
                "manage": policy.can_manage(),
                "view_moderation": policy.can_view_moderation(),
                "direct_add": policy.can_direct_add(),
                "change_avatar": policy.can_change_avatar(),
                "manage_lobby_moderators": (
                    request.user.is_superuser
                    and room.channel_kind == Room.ChannelKind.LOBBY
                ),
            },
        }
    )


@room_api
def rename_room_view(request, room_id):
    _require_post(request)
    room = rename_room(
        _room(room_id), request.user, request.profile, request.POST.get("name")
    )
    return JsonResponse({"room": room.id, "name": room.name})


@room_api
def room_avatar_view(request, room_id):
    _require_post(request)
    room = _room(room_id)
    membership = get_membership(room, request.profile)
    if not RoomPolicy(
        request.user, request.profile, room, membership
    ).can_change_avatar():
        raise RoomPermissionDenied(_("You cannot change this room's avatar."))
    if request.POST.get("remove") == "1":
        avatar = None
    else:
        form = RoomAvatarForm(request.POST, request.FILES)
        if not form.is_valid():
            errors = form.errors.get("avatar")
            message = str(errors[0]) if errors else _("Invalid room avatar.")
            raise RoomError(message, code="invalid_avatar")
        avatar = form.cleaned_data["avatar"]
    room = change_room_avatar(
        room,
        request.user,
        request.profile,
        avatar,
    )
    return JsonResponse(
        {
            "room": room.id,
            "avatar_url": room.get_avatar_url(),
            "has_custom_avatar": bool(room.avatar),
        }
    )


@room_api
def leave_room_view(request, room_id):
    _require_post(request)
    leave_room(_room(room_id), request.user, request.profile)
    return JsonResponse({"left": True, "url": reverse("chat", args=[""])})


@room_api
@transaction.atomic
def room_visibility_view(request, room_id):
    _require_post(request)
    room = _room(room_id)
    membership = UserRoom.objects.get(
        room=room,
        user=request.profile,
        state=UserRoom.State.ACTIVE,
    )
    hidden = request.POST.get("hidden") == "1"
    membership.is_hidden = hidden
    membership.hidden_at = timezone.now() if hidden else None
    update_fields = ["is_hidden", "hidden_at"]
    if not hidden:
        membership.last_read_message_id = room.last_msg_id
        membership.unread_count = 0
        membership.last_seen = timezone.now()
        update_fields.extend(["last_read_message_id", "unread_count", "last_seen"])
    membership.save(update_fields=update_fields)
    if hidden:
        transaction.on_commit(
            lambda: revoke_room_subscriptions(request.profile.id, room.id)
        )
    transaction.on_commit(
        lambda: broadcast_personal_event(
            request.profile.id,
            {
                "type": "room_visibility_changed",
                "room": room.id,
                "hidden": hidden,
            },
        )
    )
    return JsonResponse({"hidden": hidden})


@room_api
def archive_room_view(request, room_id):
    _require_post(request)
    reason = request.POST.get("reason", "").strip()
    if len(reason) > ARCHIVE_REASON_MAX_LENGTH:
        raise RoomError(
            _("Archive reason cannot exceed %(limit)s characters.")
            % {"limit": ARCHIVE_REASON_MAX_LENGTH},
            code="archive_reason_too_long",
        )
    room = archive_room(
        _room(room_id),
        request.user,
        request.profile,
        reason,
    )
    return JsonResponse({"archived": True, "room": room.id})


@room_api
def restore_room_view(request, room_id):
    _require_post(request)
    room = restore_room(_room(room_id), request.user, request.profile)
    return JsonResponse({"archived": False, "room": room.id})


@room_api
def invitation_view(request, room_id):
    room = _room(room_id)
    if request.method == "GET":
        token = get_invitation_token(room, request.user, request.profile)
    elif request.method == "POST" and request.POST.get("action") == "rotate":
        token = rotate_invitation(room, request.user, request.profile)
    elif request.method == "POST" and request.POST.get("action") == "revoke":
        revoke_invitation(room, request.user, request.profile)
        return JsonResponse({"revoked": True})
    else:
        raise RoomError(_("Invalid invitation action."))
    if token is None:
        return JsonResponse({"revoked": True})
    return JsonResponse(
        {
            "token": token,
            "url": request.build_absolute_uri(reverse("chat_invitation", args=[token])),
        }
    )


@login_required
def invitation_join_view(request, token):
    try:
        invitation = resolve_invitation(token)
        room = invitation.room
        if request.method == "POST":
            room, _ = join_from_invitation(token, request.profile)
            return redirect("chat", room.id)
        member_count = UserRoom.objects.filter(
            room=room, state=UserRoom.State.ACTIVE
        ).count()
        already_member = UserRoom.objects.filter(
            room=room,
            user=request.profile,
            state=UserRoom.State.ACTIVE,
        ).exists()
        return render(
            request,
            "chat/invitation.html",
            {
                "room": room,
                "room_avatar_url": room.get_avatar_url(),
                "member_count": member_count,
                "already_member": already_member,
                "token": token,
            },
        )
    except RoomError as error:
        return render(
            request,
            "chat/invitation.html",
            {"invitation_error": error.message},
            status=error.status,
        )


@room_api
def member_action_view(request, room_id):
    _require_post(request)
    room = _room(room_id)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()
    if action == "add":
        member_ids = _selected_member_ids(request)
        if member_ids:
            if len(member_ids) > 500:
                raise RoomError(
                    _("Select at most 500 members at a time."),
                    code="too_many_members",
                )
            added = bulk_add_room_members(
                room,
                request.user,
                request.profile,
                Profile.get_cached_instances(*member_ids),
            )
            return JsonResponse({"ok": True, "added": len(added)})
        target = _target(request)
        _, created = direct_add_member(room, request.user, request.profile, target)
        return JsonResponse({"ok": True, "added": int(created)})

    target = _target(request)
    if action == "role":
        membership = set_member_role(
            room, request.user, request.profile, target, request.POST.get("role")
        )
        return JsonResponse(
            {
                "ok": True,
                "role": membership.role,
                "manual_role": membership.manual_role,
                "synced_role": membership.synced_role,
            }
        )
    elif action == "remove":
        remove_member(room, request.user, request.profile, target, reason)
    elif action == "ban":
        ban_member(room, request.user, request.profile, target, reason)
    elif action == "unban":
        unban_member(room, request.user, request.profile, target)
    elif action == "mute":
        mute = mute_member(room, request.user, request.profile, target, reason)
        return JsonResponse({"ok": True, "expires_at": mute.expires_at.isoformat()})
    elif action == "lobby_moderator":
        if room.channel_kind != Room.ChannelKind.LOBBY:
            raise RoomError(_("This action is only available in Lobby."))
        set_lobby_moderator(
            request.user,
            request.profile,
            target,
            request.POST.get("enabled") == "1",
        )
    else:
        raise RoomError(_("Invalid member action."))
    return JsonResponse({"ok": True})


@room_api
def moderation_view(request, room_id):
    room = _room(room_id)
    membership = get_membership(room, request.profile)
    if not RoomPolicy(
        request.user, request.profile, room, membership
    ).can_view_moderation():
        return HttpResponseForbidden()
    if request.method == "POST":
        _require_post(request)
        try:
            mute_id = int(request.POST["mute_id"])
        except (KeyError, TypeError, ValueError):
            raise RoomError(_("Invalid room mute."), code="invalid_room_mute")
        revoke_room_mute(room, request.user, request.profile, mute_id)
        return JsonResponse({"ok": True})
    active_mutes = list(
        RoomMute.objects.filter(
            room=room,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).values("id", "target_id", "reason", "expires_at", "duration_days",)[:101]
    )
    bans = list(
        RoomBan.objects.filter(room=room, revoked_at__isnull=True)
        .order_by("-id")
        .values(
            "id",
            "target_id",
            "reason",
        )[:101]
    )
    moderation_profile_ids = {
        row["target_id"] for row in active_mutes + bans if row["target_id"]
    }
    moderation_profiles = {
        profile.id: profile
        for profile in Profile.get_cached_instances(*moderation_profile_ids)
    }
    if moderation_profiles:
        get_profile_public_identity.batch(
            [(profile_id,) for profile_id in moderation_profiles]
        )
    for row in active_mutes + bans:
        profile = moderation_profiles.get(row["target_id"])
        row["target_name"] = (
            profile.get_public_username(request.user) if profile else _("Deleted user")
        )
        row["target_url"] = profile.get_absolute_url() if profile else None
        row["target_css_class"] = profile.css_class if profile else ""
    return JsonResponse(
        {
            "mutes": active_mutes[:100],
            "mutes_truncated": len(active_mutes) > 100,
            "bans": bans[:100],
            "bans_truncated": len(bans) > 100,
        }
    )
