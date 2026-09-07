import logging

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Profile
from judge.utils.community import can_use_community_features

from chat_box.exceptions import RoomError, RoomPermissionDenied
from chat_box.models import Message, Room, RoomModerationLog, UserRoom
from chat_box.policies import RoomPolicy
from chat_box.services.events import broadcast_room_event
from chat_box.services.memberships import (
    activate_membership,
    bulk_add_room_members,
)
from chat_box.services.organization_sync import sync_organization_channel

logger = logging.getLogger(__name__)


def validate_room_name(name):
    name = (name or "").strip()
    if not name:
        raise RoomError(_("Room name is required."), code="name_required")
    if len(name) > 100:
        raise RoomError(
            _("Room name must be at most 100 characters."),
            code="name_too_long",
        )
    return name


def _delete_replaced_avatar(storage_name):
    if not storage_name:
        return
    try:
        default_storage.delete(storage_name)
    except Exception:
        logger.exception("Unable to delete replaced room avatar %s", storage_name)


def _check_group_creation_rate(user, profile):
    if user.is_superuser:
        return
    maximum = getattr(settings, "CHAT_GROUP_CREATION_LIMIT", 5)
    window_seconds = getattr(settings, "CHAT_GROUP_CREATION_WINDOW_SECONDS", 3600)
    since = timezone.now() - timezone.timedelta(seconds=window_seconds)
    recent = RoomModerationLog.objects.filter(
        actor=profile,
        action=RoomModerationLog.Action.CREATE,
        metadata__room_type=Room.Type.GROUP,
        created_at__gte=since,
    ).count()
    if recent >= maximum:
        raise RoomError(
            _("You have created too many groups recently. Please try again later."),
            code="group_creation_limited",
            status=429,
        )


@transaction.atomic
def create_group(user, profile, name, initial_members=()):
    if not can_use_community_features(user, profile):
        raise RoomPermissionDenied(_("Solve a problem before creating a group."))
    # Serialize the rate-limit ledger's count-and-create sequence per creator.
    Profile.objects.select_for_update().only("id").get(pk=profile.pk)
    _check_group_creation_rate(user, profile)
    room = Room.objects.create(
        room_type=Room.Type.GROUP,
        name=validate_room_name(name),
        last_activity_at=timezone.now(),
    )
    creator_membership, _created = activate_membership(
        room,
        profile,
        profile,
        role=UserRoom.Role.ADMIN,
        announce=False,
    )
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.CREATE,
        actor=profile,
        target=profile,
        metadata={"room_type": Room.Type.GROUP},
    )
    if initial_members:
        bulk_add_room_members(
            room,
            user,
            profile,
            initial_members,
            actor_membership=creator_membership,
        )
    return room


@transaction.atomic
def create_custom_channel(user, profile, name, initial_members=()):
    if not user.is_superuser:
        raise RoomPermissionDenied(
            _("Only a site administrator may create a custom channel.")
        )
    room = Room.objects.create(
        room_type=Room.Type.CHANNEL,
        channel_kind=Room.ChannelKind.CUSTOM,
        name=validate_room_name(name),
        last_activity_at=timezone.now(),
    )
    creator_membership, _created = activate_membership(
        room,
        profile,
        profile,
        role=UserRoom.Role.ADMIN,
        announce=False,
    )
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.CREATE,
        actor=profile,
        target=profile,
        metadata={
            "room_type": Room.Type.CHANNEL,
            "channel_kind": Room.ChannelKind.CUSTOM,
        },
    )
    bulk_add_room_members(
        room,
        user,
        profile,
        initial_members,
        actor_membership=creator_membership,
    )
    return room


@transaction.atomic
def create_organization_channel(user, profile, organization):
    if not organization.admins.filter(pk=profile.id).exists():
        raise RoomPermissionDenied(
            _("Only an organization administrator may create its channel.")
        )
    try:
        room = Room.objects.create(
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.ORGANIZATION,
            name=validate_room_name(organization.name),
            organization=organization,
            organization_id_snapshot=organization.id,
            organization_name_snapshot=organization.name,
            last_activity_at=timezone.now(),
        )
    except IntegrityError:
        raise RoomError(
            _("This organization already has a channel."),
            code="organization_channel_exists",
        )
    sync_organization_channel(organization)
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.CREATE,
        actor=profile,
        metadata={
            "room_type": Room.Type.CHANNEL,
            "channel_kind": Room.ChannelKind.ORGANIZATION,
            "organization_id": organization.id,
        },
    )
    return room


@transaction.atomic
def rename_room(room, user, profile, name):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, membership).can_rename():
        raise RoomPermissionDenied(_("You cannot rename this room."))
    old_name = room.name
    room.name = validate_room_name(name)
    room.save(update_fields=["name"])
    transaction.on_commit(lambda: Room.dirty_cache(room.id))
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.RENAME,
        actor=profile,
        metadata={"old_name": old_name, "new_name": room.name},
    )
    system_message = Message.objects.create(
        room=room,
        author=profile,
        body="",
        kind=Message.Kind.SYSTEM,
        system_event=Message.SystemEvent.RENAME,
        event_data={
            "actor_id": profile.id,
            "actor_name": profile.get_username(),
            "old_name": old_name,
            "new_name": room.name,
        },
    )
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {
                "type": "room_renamed",
                "room": room.id,
                "name": room.name,
                "message": system_message.id,
            },
        )
    )
    return room


@transaction.atomic
def change_room_avatar(room, user, profile, avatar):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, membership).can_change_avatar():
        raise RoomPermissionDenied(_("You cannot change this room's avatar."))

    old_avatar_name = room.avatar.name if room.avatar else None
    if avatar is None and not old_avatar_name:
        return room

    room.avatar = avatar
    room.save(update_fields=["avatar"])
    avatar_url = room.get_avatar_url()
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.AVATAR_CHANGE,
        actor=profile,
        metadata={"removed": avatar is None},
    )
    transaction.on_commit(lambda: _delete_replaced_avatar(old_avatar_name))
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {
                "type": "room_avatar_changed",
                "room": room.id,
                "room_type": room.room_type,
                "avatar_url": avatar_url,
            },
        )
    )
    return room


@transaction.atomic
def archive_room(room, user, profile, reason=""):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, membership).can_archive():
        raise RoomPermissionDenied(_("You cannot archive this room."))
    if room.archived_at is not None:
        return room
    room.archived_at = timezone.now()
    room.archived_by = profile
    room.archive_reason = reason
    room.save(update_fields=["archived_at", "archived_by", "archive_reason"])
    transaction.on_commit(lambda: Room.dirty_cache(room.id))
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.ARCHIVE,
        actor=profile,
        reason=reason,
    )
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id, {"type": "room_archived", "room": room.id}
        )
    )
    return room


@transaction.atomic
def restore_room(room, user, profile):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, membership).can_restore():
        raise RoomPermissionDenied(_("You cannot restore this room."))
    room.archived_at = None
    room.archived_by = None
    room.archive_reason = ""
    room.save(update_fields=["archived_at", "archived_by", "archive_reason"])
    transaction.on_commit(lambda: Room.dirty_cache(room.id))
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.RESTORE,
        actor=profile,
    )
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id, {"type": "room_restored", "room": room.id}
        )
    )
    return room
