from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from judge.models import Organization, Profile

from chat_box.models import Message, Room, UserRoom
from chat_box.services.events import broadcast_room_event
from chat_box.services.lobby_sync import ensure_lobby_membership
from chat_box.services.organization_sync import (
    sync_organization_profile,
    sync_organization_profiles,
)

SIGNAL_BATCH_SIZE = 500


def _organizations_by_id(organization_ids):
    return Organization.get_cached_instances(*sorted(set(organization_ids)))


def _sync_profile_across_organizations(profile, organization_ids):
    for organization in _organizations_by_id(organization_ids):
        sync_organization_profile(organization, profile.id, notify=True)


@receiver(post_save, sender=Profile)
def create_lobby_membership(sender, instance, created, **kwargs):
    if created:
        ensure_lobby_membership(instance)


@receiver(post_save, sender=User)
def sync_lobby_superuser_role(sender, instance, **kwargs):
    if hasattr(instance, "profile"):
        ensure_lobby_membership(instance.profile, notify=True)


@receiver(m2m_changed, sender=Profile.organizations.through)
def sync_profile_organization_membership(
    sender, instance, action, reverse, pk_set, **kwargs
):
    if isinstance(instance, Profile):
        if action == "pre_clear":
            instance._chat_cleared_organization_ids = list(
                instance.organizations.values_list("id", flat=True)
            )
            return
        if action not in ("post_add", "post_remove", "post_clear"):
            return
        organization_ids = set(pk_set or ())
        organization_ids.update(getattr(instance, "_chat_cleared_organization_ids", ()))
        _sync_profile_across_organizations(instance, organization_ids)
        return

    if action == "pre_clear":
        instance._chat_cleared_member_ids = list(
            instance.members.values_list("id", flat=True)
        )
        return
    if action in ("post_add", "post_remove", "post_clear"):
        profile_ids = set(pk_set or ())
        profile_ids.update(getattr(instance, "_chat_cleared_member_ids", ()))
        sync_organization_profiles(instance, profile_ids, notify=True)


def _organization_role_relation(instance, sender):
    if sender is Organization.admins.through:
        return (
            instance.admins if isinstance(instance, Organization) else instance.admin_of
        )
    return (
        instance.moderators
        if isinstance(instance, Organization)
        else instance.moderated_organizations
    )


@receiver(m2m_changed, sender=Organization.admins.through)
@receiver(m2m_changed, sender=Organization.moderators.through)
def sync_organization_chat_roles(sender, instance, action, pk_set, **kwargs):
    attribute = "_chat_cleared_role_ids_%s" % sender._meta.db_table
    if action == "pre_clear":
        setattr(
            instance,
            attribute,
            list(
                _organization_role_relation(instance, sender).values_list(
                    "id", flat=True
                )
            ),
        )
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    affected_ids = set(pk_set or ())
    affected_ids.update(getattr(instance, attribute, ()))
    if isinstance(instance, Organization):
        sync_organization_profiles(instance, affected_ids, notify=True)
    else:
        _sync_profile_across_organizations(instance, affected_ids)


@receiver(post_save, sender=Organization)
def sync_organization_channel_metadata(sender, instance, update_fields=None, **kwargs):
    room = (
        Room.objects.filter(
            organization_id=instance.id,
            channel_kind=Room.ChannelKind.ORGANIZATION,
        )
        .only("id", "name", "avatar")
        .first()
    )
    if room is None:
        return
    name_changed = room.name != instance.name
    organization_avatar_may_have_changed = not room.avatar and (
        update_fields is None or "organization_image" in update_fields
    )
    if not name_changed and not organization_avatar_may_have_changed:
        return
    transaction.on_commit(lambda: Room.dirty_cache(room.id))
    if name_changed:
        old_name = room.name
        Room.objects.filter(id=room.id).update(
            name=instance.name,
            organization_name_snapshot=instance.name,
        )
        system_message = Message.objects.create(
            room_id=room.id,
            author=None,
            body="",
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.RENAME,
            event_data={"old_name": old_name, "new_name": instance.name},
        )
        transaction.on_commit(
            lambda: broadcast_room_event(
                room.id,
                {
                    "type": "room_renamed",
                    "room": room.id,
                    "name": instance.name,
                    "message": system_message.id,
                },
            )
        )
    if organization_avatar_may_have_changed:
        avatar_url = (
            instance.organization_image.url if instance.organization_image else None
        )
        transaction.on_commit(
            lambda: broadcast_room_event(
                room.id,
                {
                    "type": "room_avatar_changed",
                    "room": room.id,
                    "room_type": Room.Type.CHANNEL,
                    "avatar_url": avatar_url,
                },
            )
        )


@receiver(pre_delete, sender=Organization)
def archive_deleted_organization_channel(sender, instance, **kwargs):
    room_id = (
        Room.objects.filter(
            organization_id=instance.id,
            channel_kind=Room.ChannelKind.ORGANIZATION,
        )
        .values_list("id", flat=True)
        .first()
    )
    if room_id is None:
        return
    Room.objects.filter(id=room_id).update(
        archived_at=timezone.now(),
        archive_reason="organization_deleted",
        organization_id_snapshot=instance.id,
        organization_name_snapshot=instance.name,
        name=instance.name,
    )
    transaction.on_commit(lambda: Room.dirty_cache(room_id))


@receiver(pre_delete, sender=Profile)
def archive_deleted_profile_direct_rooms(sender, instance, **kwargs):
    last_membership_id = 0
    while True:
        membership_rows = list(
            UserRoom.objects.filter(
                user_id=instance.id,
                state=UserRoom.State.ACTIVE,
                role=UserRoom.Role.ADMIN,
                id__gt=last_membership_id,
                room__archived_at__isnull=True,
            )
            .filter(
                Q(room__room_type=Room.Type.GROUP)
                | Q(room__channel_kind=Room.ChannelKind.CUSTOM)
            )
            .order_by("id")
            .values_list("id", "room_id")[:SIGNAL_BATCH_SIZE]
        )
        if not membership_rows:
            break
        last_membership_id = membership_rows[-1][0]
        candidate_room_ids = [room_id for _, room_id in membership_rows]
        locked_room_ids = list(
            Room.objects.select_for_update()
            .filter(id__in=candidate_room_ids, archived_at__isnull=True)
            .order_by("id")
            .values_list("id", flat=True)
        )
        room_ids_with_other_admin = set(
            UserRoom.objects.filter(
                room_id__in=locked_room_ids,
                state=UserRoom.State.ACTIVE,
                role=UserRoom.Role.ADMIN,
            )
            .exclude(user_id=instance.id)
            .values_list("room_id", flat=True)
        )
        room_ids = [
            room_id
            for room_id in locked_room_ids
            if room_id not in room_ids_with_other_admin
        ]
        if not room_ids:
            continue
        Room.objects.filter(id__in=room_ids).update(
            archived_at=timezone.now(),
            archive_reason="last_admin_deleted",
        )
        transaction.on_commit(
            lambda room_ids=tuple(room_ids): Room.dirty_cache(*room_ids)
        )
        for room_id in room_ids:
            transaction.on_commit(
                lambda room_id=room_id: broadcast_room_event(
                    room_id,
                    {"type": "room_archived", "room": room_id},
                )
            )

    last_room_id = 0
    while True:
        room_ids = list(
            Room.objects.filter(
                Q(direct_user_low_id=instance.id) | Q(direct_user_high_id=instance.id),
                room_type=Room.Type.DIRECT,
                id__gt=last_room_id,
            )
            .order_by("id")
            .values_list("id", flat=True)[:SIGNAL_BATCH_SIZE]
        )
        if not room_ids:
            break
        Room.objects.filter(id__in=room_ids).update(
            archived_at=timezone.now(),
            archive_reason="profile_deleted",
        )
        transaction.on_commit(
            lambda room_ids=tuple(room_ids): Room.dirty_cache(*room_ids)
        )
        last_room_id = room_ids[-1]
