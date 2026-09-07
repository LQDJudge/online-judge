from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from judge.models import Profile
from judge.models.notification import Notification, NotificationCategory

from chat_box.exceptions import RoomPermissionDenied
from chat_box.models import Room, UserRoom
from chat_box.services.events import broadcast_personal_event, broadcast_room_event

LOBBY_SYNC_BATCH_SIZE = 500


@transaction.atomic
def ensure_lobby():
    lobby = Room.objects.select_for_update().filter(singleton_key="lobby").first()
    if lobby is not None:
        return lobby
    try:
        with transaction.atomic():
            return Room.objects.create(
                room_type=Room.Type.CHANNEL,
                channel_kind=Room.ChannelKind.LOBBY,
                name="Lobby",
                singleton_key="lobby",
            )
    except IntegrityError:
        return Room.objects.select_for_update().get(singleton_key="lobby")


@transaction.atomic
def ensure_lobby_membership(profile, *, notify=False):
    lobby = ensure_lobby()
    membership, _ = UserRoom.objects.select_for_update().get_or_create(
        room=lobby,
        user=profile,
        defaults={
            "state": UserRoom.State.ACTIVE,
            "role": UserRoom.Role.MEMBER,
            "manual_role": UserRoom.Role.MEMBER,
            "synced_role": UserRoom.Role.MEMBER,
            "last_read_message_id": lobby.last_msg_id,
        },
    )
    original = (
        membership.state,
        membership.role,
        membership.manual_role,
        membership.synced_role,
        membership.deactivated_at,
    )
    if profile.user.is_superuser:
        role = UserRoom.Role.ADMIN
        synced_role = UserRoom.Role.ADMIN
    elif membership.manual_role == UserRoom.Role.MODERATOR:
        role = UserRoom.Role.MODERATOR
        synced_role = UserRoom.Role.MODERATOR
    else:
        role = UserRoom.Role.MEMBER
        synced_role = UserRoom.Role.MEMBER
        membership.manual_role = UserRoom.Role.MEMBER
    membership.state = UserRoom.State.ACTIVE
    membership.role = role
    membership.synced_role = synced_role
    membership.deactivated_at = None
    current = (
        membership.state,
        membership.role,
        membership.manual_role,
        membership.synced_role,
        membership.deactivated_at,
    )
    if current != original:
        membership.save(
            update_fields=[
                "state",
                "role",
                "manual_role",
                "synced_role",
                "deactivated_at",
            ]
        )
        if notify:
            transaction.on_commit(
                lambda: broadcast_personal_event(
                    profile.id,
                    {"type": "room_membership_changed", "room": lobby.id},
                )
            )
            transaction.on_commit(
                lambda: broadcast_room_event(
                    lobby.id,
                    {"type": "membership_sync", "room": lobby.id},
                )
            )
    return membership


def sync_lobby_memberships(*, dry_run=False, batch_size=LOBBY_SYNC_BATCH_SIZE):
    """Reconcile all Lobby memberships in bounded, idempotent batches."""
    batch_size = max(1, min(int(batch_size), LOBBY_SYNC_BATCH_SIZE))
    lobby = ensure_lobby()
    report = {"created": 0, "updated": 0, "checked": 0, "batch_size": batch_size}
    last_profile_id = 0
    while True:
        profile_ids = list(
            Profile.objects.filter(id__gt=last_profile_id)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if not profile_ids:
            break
        last_profile_id = profile_ids[-1]
        superuser_ids = set(
            Profile.objects.filter(
                id__in=profile_ids,
                user__is_superuser=True,
            ).values_list("id", flat=True)
        )
        memberships = {
            membership.user_id: membership
            for membership in UserRoom.objects.filter(
                room=lobby,
                user_id__in=profile_ids,
            ).order_by("id")
        }
        creates = []
        updates = []
        now = timezone.now()
        for profile_id in profile_ids:
            membership = memberships.get(profile_id)
            expected_role = (
                UserRoom.Role.ADMIN
                if profile_id in superuser_ids
                else (
                    UserRoom.Role.MODERATOR
                    if membership and membership.manual_role == UserRoom.Role.MODERATOR
                    else UserRoom.Role.MEMBER
                )
            )
            expected_manual_role = (
                membership.manual_role
                if membership
                and membership.manual_role == UserRoom.Role.MODERATOR
                and profile_id not in superuser_ids
                else UserRoom.Role.MEMBER
            )
            if membership is None:
                report["created"] += 1
                if not dry_run:
                    creates.append(
                        UserRoom(
                            room=lobby,
                            user_id=profile_id,
                            state=UserRoom.State.ACTIVE,
                            role=expected_role,
                            manual_role=expected_manual_role,
                            synced_role=expected_role,
                            last_read_message_id=lobby.last_msg_id,
                        )
                    )
                continue
            changed = (
                membership.state != UserRoom.State.ACTIVE
                or membership.role != expected_role
                or membership.manual_role != expected_manual_role
                or membership.synced_role != expected_role
                or membership.deactivated_at is not None
            )
            if changed:
                report["updated"] += 1
                if not dry_run:
                    reactivating = membership.state != UserRoom.State.ACTIVE
                    membership.state = UserRoom.State.ACTIVE
                    membership.role = expected_role
                    membership.manual_role = expected_manual_role
                    membership.synced_role = expected_role
                    membership.deactivated_at = None
                    if reactivating:
                        membership.activated_at = now
                        membership.last_read_message_id = lobby.last_msg_id
                        membership.unread_count = 0
                        membership.is_hidden = False
                        membership.hidden_at = None
                    updates.append(membership)
        if creates:
            UserRoom.objects.bulk_create(creates, batch_size=batch_size)
        if updates:
            UserRoom.objects.bulk_update(
                updates,
                [
                    "state",
                    "role",
                    "manual_role",
                    "synced_role",
                    "deactivated_at",
                    "activated_at",
                    "last_read_message_id",
                    "unread_count",
                    "is_hidden",
                    "hidden_at",
                ],
                batch_size=batch_size,
            )
        report["checked"] += len(profile_ids)
    return report


@transaction.atomic
def set_lobby_moderator(actor_user, actor, target, enabled):
    if not actor_user.is_superuser:
        raise RoomPermissionDenied(
            _("Only site administrators may manage Lobby moderators.")
        )
    membership = ensure_lobby_membership(target)
    if target.user.is_superuser:
        return membership
    previous_role = membership.role
    membership.manual_role = (
        UserRoom.Role.MODERATOR if enabled else UserRoom.Role.MEMBER
    )
    membership.synced_role = membership.manual_role
    membership.role = membership.manual_role
    membership.save(update_fields=["manual_role", "synced_role", "role"])
    if membership.role != previous_role:
        if enabled:
            summary = _("You were appointed as a Lobby moderator.")
            event_type = "lobby_moderator_appointed"
        else:
            summary = _("You are no longer a Lobby moderator.")
            event_type = "lobby_moderator_revoked"
        Notification.objects.create_notification(
            owner=target,
            category=NotificationCategory.ROOM_MEMBERSHIP,
            html_link=format_html(
                '<a href="{}">{}</a>',
                reverse("chat", kwargs={"room_id": membership.room_id}),
                summary,
            ),
            author=actor,
            extra_data={
                "type": event_type,
                "room_id": membership.room_id,
                "room_name": "Lobby",
            },
            deduplicate=False,
        )
        transaction.on_commit(
            lambda: broadcast_personal_event(
                target.id,
                {"type": "room_membership_changed", "room": membership.room_id},
            )
        )
        transaction.on_commit(
            lambda: broadcast_room_event(
                membership.room_id,
                {"type": "membership_sync", "room": membership.room_id},
            )
        )
    return membership
