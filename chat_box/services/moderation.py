from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from judge.models.notification import Notification, NotificationCategory

from chat_box.exceptions import RoomError, RoomPermissionDenied
from chat_box.models import Message, Room, RoomModerationLog, RoomMute, UserRoom
from chat_box.policies import RoomPolicy
from chat_box.services.events import broadcast_personal_event, broadcast_room_event

ROOM_MUTE_MAX_DAYS = 30


@transaction.atomic
def hide_message(message, user, profile, reason=""):
    room = Room.objects.select_for_update().get(pk=message.room_id)
    message = Message.objects.select_for_update().get(pk=message.pk, room=room)
    actor_membership = UserRoom.objects.filter(room=room, user=profile).first()
    target_membership = (
        UserRoom.objects.filter(room=room, user_id=message.author_id).first()
        if message.author_id
        else None
    )
    if not RoomPolicy(user, profile, room, actor_membership).can_hide_message(
        message, target_membership
    ):
        raise RoomPermissionDenied(_("You cannot hide this message."))
    if message.hidden:
        return message
    message.hidden = True
    message.save(update_fields=["hidden"])
    if room.last_msg_id == message.id:
        replacement = (
            Message.objects.filter(
                room=room,
                hidden=False,
                kind=Message.Kind.USER,
            )
            .order_by("-id")
            .values("id", "time")
            .first()
        )
        room.last_msg_id = replacement["id"] if replacement else None
        room.last_activity_at = replacement["time"] if replacement else None
        room.save(update_fields=["last_msg_id", "last_activity_at"])
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.HIDE_MESSAGE,
        actor=profile,
        target_id=message.author_id,
        message=message,
        reason=reason,
    )
    transaction.on_commit(lambda: Room.dirty_cache(room.id))
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {"type": "message_hidden", "room": room.id, "message": message.id},
        )
    )
    return message


@transaction.atomic
def mute_member(room, user, profile, target, reason):
    room = Room.objects.select_for_update().get(pk=room.pk)
    memberships = list(
        UserRoom.objects.select_for_update()
        .filter(room=room, user_id__in=[profile.id, target.id])
        .order_by("pk")
    )
    by_user = {membership.user_id: membership for membership in memberships}
    policy = RoomPolicy(user, profile, room, by_user.get(profile.id))
    if not policy.can_moderate_target(by_user.get(target.id)):
        raise RoomPermissionDenied(_("You cannot mute this member."))
    if not reason and not user.is_superuser:
        raise RoomError(_("Reason is required."), code="reason_required")
    now = timezone.now()
    RoomMute.objects.select_for_update().filter(
        room=room,
        target=target,
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).update(revoked_at=now, revoked_by=profile)
    prior_count = RoomMute.objects.filter(room=room, target=target).count()
    duration_days = min(prior_count + 1, ROOM_MUTE_MAX_DAYS)
    mute = RoomMute.objects.create(
        room=room,
        target=target,
        muted_by=profile,
        reason=reason,
        duration_days=duration_days,
        expires_at=now + timezone.timedelta(days=duration_days),
    )
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.MUTE,
        actor=profile,
        target=target,
        reason=reason,
        metadata={
            "duration_days": duration_days,
            "expires_at": mute.expires_at.isoformat(),
        },
    )
    room_url = reverse("chat", kwargs={"room_id": room.id})
    mute_until_display = timezone.localtime(mute.expires_at).strftime("%Y-%m-%d %H:%M")
    summary = format_html(
        '<a href="{}">{}</a>',
        room_url,
        _("You were muted in %(room)s until %(time)s.")
        % {"room": room.name, "time": mute_until_display},
    )
    if reason:
        summary = format_html(
            "{}<br>{}",
            summary,
            _("Reason: %(reason)s") % {"reason": reason},
        )
    Notification.objects.create_notification(
        owner=target,
        category=NotificationCategory.CHAT_MUTE,
        html_link=summary,
        author=profile,
        extra_data={
            "type": "room_mute_notice",
            "room_id": room.id,
            "room_name": room.name,
            "mute_until": mute.expires_at.isoformat(),
            "mute_until_display": mute_until_display,
            "reason": reason,
        },
        deduplicate=False,
    )
    transaction.on_commit(
        lambda: broadcast_personal_event(
            target.id,
            {
                "type": "room_muted",
                "room": room.id,
                "expires_at": mute.expires_at.isoformat(),
            },
        )
    )
    return mute


@transaction.atomic
def revoke_room_mute(room, user, profile, mute_id):
    room = Room.objects.select_for_update().get(pk=room.pk)
    actor_membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, actor_membership).can_view_moderation():
        raise RoomPermissionDenied(_("You cannot manage mutes in this room."))
    mute = RoomMute.objects.select_for_update().get(
        pk=mute_id,
        room=room,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    )
    mute.revoked_at = timezone.now()
    mute.revoked_by = profile
    mute.save(update_fields=["revoked_at", "revoked_by"])
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.UNMUTE,
        actor=profile,
        target=mute.target,
    )
    Notification.objects.create_notification(
        owner=mute.target,
        category=NotificationCategory.CHAT_MUTE,
        html_link=format_html(
            '<a href="{}">{}</a>',
            reverse("chat", kwargs={"room_id": room.id}),
            _("Your mute in %(room)s was removed.") % {"room": room.name},
        ),
        author=profile,
        extra_data={
            "type": "room_unmuted",
            "room_id": room.id,
            "room_name": room.name,
        },
        deduplicate=False,
    )
    transaction.on_commit(
        lambda: broadcast_personal_event(
            mute.target_id,
            {"type": "room_unmuted", "room": room.id},
        )
    )
    return mute
