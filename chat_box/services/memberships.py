from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from judge.models import Profile
from judge.models.notification import (
    Notification,
    NotificationCategory,
    NotificationProfile,
    unseen_notifications_count,
)

from chat_box.exceptions import RoomError, RoomPermissionDenied
from chat_box.models import Message, Room, RoomBan, RoomModerationLog, UserRoom
from chat_box.policies import RoomPolicy, highest_role
from chat_box.services.events import (
    broadcast_personal_event,
    broadcast_personal_events,
    broadcast_room_event,
    revoke_room_subscriptions,
)
from chat_box.services.organization_sync import (
    organization_role,
    sync_organization_profile,
)

GROUP_MEMBER_LIMIT = 50


def _normal_last_admin_rule(room):
    return room.room_type == Room.Type.GROUP or (
        room.room_type == Room.Type.CHANNEL
        and room.channel_kind == Room.ChannelKind.CUSTOM
    )


def _check_last_admin(room, membership):
    if not _normal_last_admin_rule(room) or membership.role != UserRoom.Role.ADMIN:
        return
    other_admin_exists = (
        UserRoom.objects.filter(
            room=room,
            state=UserRoom.State.ACTIVE,
            role=UserRoom.Role.ADMIN,
        )
        .exclude(pk=membership.pk)
        .exists()
    )
    if not other_admin_exists:
        raise RoomError(
            _("Assign another administrator before changing the last administrator."),
            code="last_admin_required",
        )


def _create_system_event(room, actor, event_name, data):
    message = Message.objects.create(
        room=room,
        author=actor,
        body="",
        kind=Message.Kind.SYSTEM,
        system_event=event_name,
        event_data=data,
    )
    event_payload = {
        "type": "message",
        "room": room.id,
        "message": message.id,
        "notifies": False,
    }
    if (
        event_name in (Message.SystemEvent.JOIN, Message.SystemEvent.LEAVE)
        and room.channel_kind != Room.ChannelKind.LOBBY
    ):
        event_payload["member_count"] = UserRoom.objects.filter(
            room=room, state=UserRoom.State.ACTIVE
        ).count()
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            event_payload,
        )
    )
    return message


def _notify_membership_change(
    target,
    room,
    actor,
    summary,
    event_type,
    *,
    category=NotificationCategory.ROOM_MEMBERSHIP,
    link_to_room=True,
    extra_data=None,
):
    html_link = format_html("{}", summary)
    if link_to_room:
        room_url = reverse("chat", kwargs={"room_id": room.id})
        html_link = format_html('<a href="{}">{}</a>', room_url, summary)
    notification_data = {
        "type": event_type,
        "room_id": room.id,
        "room_name": room.name,
    }
    notification_data.update(extra_data or {})
    Notification.objects.create_notification(
        owner=target,
        category=category,
        html_link=html_link,
        author=actor,
        extra_data=notification_data,
        deduplicate=False,
    )
    transaction.on_commit(
        lambda: broadcast_personal_event(
            target.id,
            {"type": event_type, "room": room.id},
        )
    )


def _bulk_notify_membership_change(targets, room, actor, summary, event_type):
    targets = list({target.id: target for target in targets}.values())
    if not targets:
        return
    room_url = reverse("chat", kwargs={"room_id": room.id})
    html_link = format_html('<a href="{}">{}</a>', room_url, summary)
    Notification.objects.bulk_create(
        [
            Notification(
                owner=target,
                category=NotificationCategory.ROOM_MEMBERSHIP,
                html_link=html_link,
                author=actor,
                extra_data={
                    "type": event_type,
                    "room_id": room.id,
                    "room_name": room.name,
                },
            )
            for target in targets
        ],
        batch_size=500,
    )
    target_ids = [target.id for target in targets]
    existing_profile_ids = set(
        NotificationProfile.objects.filter(user_id__in=target_ids).values_list(
            "user_id", flat=True
        )
    )
    NotificationProfile.objects.bulk_create(
        [
            NotificationProfile(user=target)
            for target in targets
            if target.id not in existing_profile_ids
        ],
        ignore_conflicts=True,
        batch_size=500,
    )
    NotificationProfile.objects.filter(user_id__in=target_ids).update(
        unread_count=F("unread_count") + 1
    )
    unseen_notifications_count.dirty_multi([(target,) for target in targets])
    transaction.on_commit(
        lambda: broadcast_personal_events(
            target_ids,
            {"type": event_type, "room": room.id},
        )
    )


@transaction.atomic
def activate_membership(
    room,
    target,
    actor,
    *,
    role=UserRoom.Role.MEMBER,
    announce=True,
):
    room = Room.objects.select_for_update().get(pk=room.pk)
    if room.archived_at is not None:
        raise RoomError(_("This room is archived."), code="room_archived")
    if RoomBan.objects.filter(
        room=room, target=target, revoked_at__isnull=True
    ).exists():
        raise RoomPermissionDenied(
            _("You are blocked from this room."), code="room_banned"
        )
    membership = (
        UserRoom.objects.select_for_update().filter(room=room, user=target).first()
    )
    if membership and membership.state == UserRoom.State.ACTIVE:
        return membership, False
    if room.room_type == Room.Type.GROUP:
        member_count = UserRoom.objects.filter(
            room=room, state=UserRoom.State.ACTIVE
        ).count()
        if member_count >= GROUP_MEMBER_LIMIT:
            raise RoomError(
                _(
                    "Groups support at most 50 members. Create a channel for a larger community."
                ),
                code="group_full",
            )
    now = timezone.now()
    if membership is None:
        membership = UserRoom(room=room, user=target)
    membership.state = UserRoom.State.ACTIVE
    membership.role = role
    membership.manual_role = role
    membership.synced_role = None
    membership.activated_at = now
    membership.deactivated_at = None
    membership.is_hidden = False
    membership.hidden_at = None
    membership.last_read_message_id = room.last_msg_id
    membership.unread_count = 0
    membership.save()
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.JOIN,
        actor=actor,
        target=target,
    )
    if announce:
        _create_system_event(
            room,
            actor,
            Message.SystemEvent.JOIN,
            {"user_id": target.id, "username": target.get_username()},
        )
    return membership, True


@transaction.atomic
def leave_room(room, actor_user, actor):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.select_for_update().get(room=room, user=actor)
    policy = RoomPolicy(actor_user, actor, room, membership)
    if not policy.can_leave() or room.room_type == Room.Type.DIRECT:
        raise RoomPermissionDenied(_("You cannot leave this room."))
    _check_last_admin(room, membership)
    membership.state = UserRoom.State.LEFT
    membership.role = None
    membership.manual_role = None
    membership.synced_role = None
    membership.deactivated_at = timezone.now()
    membership.is_hidden = False
    membership.hidden_at = None
    membership.save(
        update_fields=[
            "state",
            "role",
            "manual_role",
            "synced_role",
            "deactivated_at",
            "is_hidden",
            "hidden_at",
        ]
    )
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.LEAVE,
        actor=actor,
        target=actor,
    )
    _create_system_event(
        room,
        actor,
        Message.SystemEvent.LEAVE,
        {"user_id": actor.id, "username": actor.get_username()},
    )
    transaction.on_commit(lambda: revoke_room_subscriptions(actor.id, room.id))
    return membership


def _load_management_context(room, actor_user, actor, target):
    room = Room.objects.select_for_update().get(pk=room.pk)
    memberships = list(
        UserRoom.objects.select_for_update()
        .filter(room=room, user_id__in=[actor.id, target.id])
        .order_by("pk")
    )
    by_user = {membership.user_id: membership for membership in memberships}
    actor_membership = by_user.get(actor.id)
    target_membership = by_user.get(target.id)
    return room, actor_membership, target_membership


@transaction.atomic
def direct_add_member(room, actor_user, actor, target):
    room, actor_membership, target_membership = _load_management_context(
        room, actor_user, actor, target
    )
    policy = RoomPolicy(actor_user, actor, room, actor_membership)
    if not policy.can_direct_add():
        raise RoomPermissionDenied(
            _("Only a site administrator may directly add users.")
        )
    if room.channel_kind == Room.ChannelKind.ORGANIZATION and not organization_role(
        room.organization, target.id
    ):
        raise RoomPermissionDenied(
            _("This user is not eligible for the organization channel.")
        )
    if RoomBan.objects.filter(
        room=room,
        target=target,
        revoked_at__isnull=True,
    ).exists():
        raise RoomPermissionDenied(
            _("This user is blocked from this room."),
            code="room_banned",
        )
    was_active = bool(
        target_membership and target_membership.state == UserRoom.State.ACTIVE
    )
    if room.channel_kind == Room.ChannelKind.ORGANIZATION:
        membership = sync_organization_profile(
            room.organization,
            target.id,
            activate=True,
        )
        created = not was_active and membership.state == UserRoom.State.ACTIVE
    else:
        membership, created = activate_membership(room, target, actor, announce=True)
    if created:
        if room.channel_kind == Room.ChannelKind.ORGANIZATION:
            _create_system_event(
                room,
                actor,
                Message.SystemEvent.JOIN,
                {"user_id": target.id, "username": target.get_username()},
            )
        RoomModerationLog.objects.create(
            room=room,
            action=RoomModerationLog.Action.ADD,
            actor=actor,
            target=target,
        )
        _notify_membership_change(
            target,
            room,
            actor,
            _("You were added to %(room)s.") % {"room": room.name},
            "room_added",
        )
    return membership, created


@transaction.atomic
def rejoin_organization_channel(room, actor):
    room = Room.objects.select_for_update().get(pk=room.pk)
    if (
        room.channel_kind != Room.ChannelKind.ORGANIZATION
        or room.organization_id is None
        or not organization_role(room.organization, actor.id)
    ):
        raise RoomPermissionDenied(_("You cannot join this organization channel."))
    if room.archived_at is not None:
        raise RoomError(_("This room is archived."), code="room_archived")
    if RoomBan.objects.filter(
        room=room,
        target=actor,
        revoked_at__isnull=True,
    ).exists():
        raise RoomPermissionDenied(
            _("You are blocked from this room."),
            code="room_banned",
        )
    current_membership = (
        UserRoom.objects.select_for_update().filter(room=room, user=actor).first()
    )
    was_active = bool(
        current_membership and current_membership.state == UserRoom.State.ACTIVE
    )
    membership = sync_organization_profile(
        room.organization,
        actor.id,
        activate=True,
    )
    if not membership or membership.state != UserRoom.State.ACTIVE:
        raise RoomPermissionDenied(_("You cannot join this organization channel."))
    if not was_active:
        RoomModerationLog.objects.create(
            room=room,
            action=RoomModerationLog.Action.JOIN,
            actor=actor,
            target=actor,
        )
        _create_system_event(
            room,
            actor,
            Message.SystemEvent.JOIN,
            {"user_id": actor.id, "username": actor.get_username()},
        )
        transaction.on_commit(
            lambda: broadcast_personal_event(
                actor.id,
                {"type": "room_membership_changed", "room": room.id},
            )
        )
    return membership, not was_active


@transaction.atomic
def bulk_add_room_members(room, actor_user, actor, targets, *, actor_membership=None):
    """Activate selected group/custom-channel members without N+1 writes."""
    room = Room.objects.select_for_update().get(pk=room.pk)
    if actor_membership is None:
        actor_membership = UserRoom.objects.filter(room=room, user=actor).first()
    policy = RoomPolicy(actor_user, actor, room, actor_membership)
    is_supported_room = room.room_type == Room.Type.GROUP or (
        room.room_type == Room.Type.CHANNEL
        and room.channel_kind == Room.ChannelKind.CUSTOM
    )
    if not is_supported_room or not policy.can_direct_add():
        raise RoomPermissionDenied(
            _("Only a site administrator may directly add users.")
        )
    targets = list(
        {target.id: target for target in targets if target.id != actor.id}.values()
    )
    if not targets:
        return []
    target_ids = [target.id for target in targets]
    banned_ids = set(
        RoomBan.objects.filter(
            room=room,
            target_id__in=target_ids,
            revoked_at__isnull=True,
        ).values_list("target_id", flat=True)
    )
    memberships = {
        membership.user_id: membership
        for membership in UserRoom.objects.select_for_update()
        .filter(room=room, user_id__in=target_ids)
        .order_by("id")
    }
    if room.room_type == Room.Type.GROUP:
        active_count = UserRoom.objects.filter(
            room=room,
            state=UserRoom.State.ACTIVE,
        ).count()
        new_member_count = sum(
            1
            for target in targets
            if target.id not in banned_ids
            and (
                target.id not in memberships
                or memberships[target.id].state != UserRoom.State.ACTIVE
            )
        )
        if active_count + new_member_count > GROUP_MEMBER_LIMIT:
            raise RoomError(
                _(
                    "Groups support at most 50 members. Create a channel for a larger community."
                ),
                code="group_full",
            )
    now = timezone.now()
    creates = []
    updates = []
    activated_targets = []
    for target in targets:
        if target.id in banned_ids:
            continue
        membership = memberships.get(target.id)
        if membership and membership.state == UserRoom.State.ACTIVE:
            continue
        if membership is None:
            membership = UserRoom(room=room, user=target)
            creates.append(membership)
        else:
            updates.append(membership)
        membership.state = UserRoom.State.ACTIVE
        membership.role = UserRoom.Role.MEMBER
        membership.manual_role = UserRoom.Role.MEMBER
        membership.synced_role = None
        membership.activated_at = now
        membership.deactivated_at = None
        membership.last_read_message_id = room.last_msg_id
        membership.unread_count = 0
        membership.is_hidden = False
        membership.hidden_at = None
        activated_targets.append(target)
    if creates:
        UserRoom.objects.bulk_create(creates, batch_size=500)
    if updates:
        UserRoom.objects.bulk_update(
            updates,
            [
                "state",
                "role",
                "manual_role",
                "synced_role",
                "activated_at",
                "deactivated_at",
                "last_read_message_id",
                "unread_count",
                "is_hidden",
                "hidden_at",
            ],
            batch_size=500,
        )
    RoomModerationLog.objects.bulk_create(
        [
            RoomModerationLog(
                room=room,
                action=RoomModerationLog.Action.ADD,
                actor=actor,
                target=target,
            )
            for target in activated_targets
        ],
        batch_size=500,
    )
    _bulk_notify_membership_change(
        activated_targets,
        room,
        actor,
        _("You were added to %(room)s.") % {"room": room.name},
        "room_added",
    )
    if activated_targets:
        usernames = dict(
            Profile.objects.filter(
                id__in=[target.id for target in activated_targets]
            ).values_list("id", "user__username")
        )
        _create_system_event(
            room,
            actor,
            Message.SystemEvent.JOIN,
            {
                "user_ids": [target.id for target in activated_targets],
                "usernames": [
                    usernames[target.id]
                    for target in activated_targets
                    if target.id in usernames
                ],
            },
        )
    return activated_targets


@transaction.atomic
def set_member_role(room, actor_user, actor, target, role):
    if role not in UserRoom.Role.values:
        raise RoomError(_("Invalid room role."), code="invalid_role")
    room, actor_membership, target_membership = _load_management_context(
        room, actor_user, actor, target
    )
    policy = RoomPolicy(actor_user, actor, room, actor_membership)
    if not target_membership or not policy.can_change_role(target_membership):
        raise RoomPermissionDenied(_("You cannot change this member's role."))
    if target_membership.role == UserRoom.Role.ADMIN and role != UserRoom.Role.ADMIN:
        _check_last_admin(room, target_membership)
    previous_role = target_membership.role
    previous_manual_role = target_membership.manual_role
    target_membership.manual_role = role
    target_membership.role = (
        highest_role(role, target_membership.synced_role)
        if room.channel_kind == Room.ChannelKind.ORGANIZATION
        else role
    )
    if (
        previous_role == target_membership.role
        and previous_manual_role == target_membership.manual_role
    ):
        return target_membership
    target_membership.save(update_fields=["manual_role", "role"])
    RoomModerationLog.objects.create(
        room=room,
        action=(
            RoomModerationLog.Action.PROMOTE
            if target_membership.role
            and previous_role
            and target_membership.role != previous_role
            and target_membership.role in (UserRoom.Role.ADMIN, UserRoom.Role.MODERATOR)
            else RoomModerationLog.Action.DEMOTE
        ),
        actor=actor,
        target=target,
        metadata={"from": previous_role, "to": target_membership.role},
    )
    _notify_membership_change(
        target,
        room,
        actor,
        _("Your role in %(room)s is now %(role)s.")
        % {"room": room.name, "role": target_membership.get_role_display()},
        "room_role_changed",
        category=NotificationCategory.ROOM_MEMBERSHIP,
        extra_data={"role": target_membership.role},
    )
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {
                "type": "member_role",
                "room": room.id,
                "user": target.id,
                "role": target_membership.role,
            },
        )
    )
    return target_membership


def _deactivate_member(room, actor_user, actor, target, reason, state, action):
    room, actor_membership, target_membership = _load_management_context(
        room, actor_user, actor, target
    )
    policy = RoomPolicy(actor_user, actor, room, actor_membership)
    if not target_membership or not policy.can_remove_or_ban(target_membership):
        raise RoomPermissionDenied(_("You cannot remove this member."))
    if not reason and not actor_user.is_superuser:
        raise RoomError(_("Reason is required."), code="reason_required")
    _check_last_admin(room, target_membership)
    target_membership.state = state
    target_membership.role = None
    target_membership.manual_role = None
    target_membership.synced_role = None
    target_membership.deactivated_at = timezone.now()
    target_membership.save(
        update_fields=[
            "state",
            "role",
            "manual_role",
            "synced_role",
            "deactivated_at",
        ]
    )
    RoomModerationLog.objects.create(
        room=room,
        action=action,
        actor=actor,
        target=target,
        reason=reason,
    )
    if action == RoomModerationLog.Action.BAN:
        summary = _("You were blocked from %(room)s.") % {"room": room.name}
        event_type = "room_blocked"
    else:
        summary = _("You were removed from %(room)s.") % {"room": room.name}
        event_type = "room_removed"
    _notify_membership_change(
        target,
        room,
        actor,
        summary,
        event_type,
        category=NotificationCategory.ROOM_MEMBERSHIP,
        link_to_room=False,
    )
    transaction.on_commit(lambda: revoke_room_subscriptions(target.id, room.id))
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {
                "type": "member_removed",
                "room": room.id,
                "user": target.id,
                "member_count": UserRoom.objects.filter(
                    room=room,
                    state=UserRoom.State.ACTIVE,
                ).count(),
            },
        )
    )
    return room, target_membership


@transaction.atomic
def remove_member(room, actor_user, actor, target, reason):
    return _deactivate_member(
        room,
        actor_user,
        actor,
        target,
        reason,
        UserRoom.State.REMOVED,
        RoomModerationLog.Action.REMOVE,
    )[1]


@transaction.atomic
def ban_member(room, actor_user, actor, target, reason):
    room, membership = _deactivate_member(
        room,
        actor_user,
        actor,
        target,
        reason,
        UserRoom.State.REMOVED,
        RoomModerationLog.Action.BAN,
    )
    RoomBan.objects.filter(room=room, target=target, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_by=actor
    )
    RoomBan.objects.create(room=room, target=target, banned_by=actor, reason=reason)
    return membership


@transaction.atomic
def unban_member(room, actor_user, actor, target):
    room = Room.objects.select_for_update().get(pk=room.pk)
    actor_membership = UserRoom.objects.filter(room=room, user=actor).first()
    if not RoomPolicy(actor_user, actor, room, actor_membership).can_manage():
        raise RoomPermissionDenied(_("You cannot unblock members in this room."))
    bans = RoomBan.objects.select_for_update().filter(
        room=room, target=target, revoked_at__isnull=True
    )
    if not bans.exists():
        raise RoomError(_("This user is not blocked."), code="not_banned")
    bans.update(revoked_at=timezone.now(), revoked_by=actor)
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.UNBAN,
        actor=actor,
        target=target,
    )
    _notify_membership_change(
        target,
        room,
        actor,
        _("You were unblocked from %(room)s.") % {"room": room.name},
        "room_unblocked",
        category=NotificationCategory.ROOM_MEMBERSHIP,
        link_to_room=False,
    )
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {"type": "moderation_changed", "room": room.id},
        )
    )
