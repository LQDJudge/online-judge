from django.db import transaction
from django.utils import timezone

from chat_box.models import Room, RoomBan, UserRoom
from chat_box.policies import highest_role
from chat_box.services.events import (
    broadcast_personal_events,
    broadcast_room_event,
    revoke_room_subscriptions_many,
)

SYNC_BATCH_SIZE = 500


def _batched(values, size=SYNC_BATCH_SIZE):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _relation_ids(manager):
    """Read an organization relation in keyset batches below the SQL time limit."""
    result = set()
    last_profile_id = 0
    while True:
        profile_ids = list(
            manager.filter(id__gt=last_profile_id)
            .order_by("id")
            .values_list("id", flat=True)[:SYNC_BATCH_SIZE]
        )
        if not profile_ids:
            return result
        result.update(profile_ids)
        last_profile_id = profile_ids[-1]


def organization_user_ids(organization):
    return _relation_ids(organization.admins).union(
        _relation_ids(organization.moderators),
        _relation_ids(organization.members),
    )


def _organization_roles_for_ids(organization, profile_ids):
    profile_ids = list(profile_ids)
    roles = {
        profile_id: UserRoom.Role.MEMBER
        for profile_id in organization.members.filter(id__in=profile_ids).values_list(
            "id", flat=True
        )
    }
    roles.update(
        {
            profile_id: UserRoom.Role.MODERATOR
            for profile_id in organization.moderators.filter(
                id__in=profile_ids
            ).values_list("id", flat=True)
        }
    )
    roles.update(
        {
            profile_id: UserRoom.Role.ADMIN
            for profile_id in organization.admins.filter(
                id__in=profile_ids
            ).values_list("id", flat=True)
        }
    )
    return roles


def organization_role(organization, profile_id):
    if organization is None:
        return None
    return _organization_roles_for_ids(organization, [profile_id]).get(profile_id)


@transaction.atomic
def _sync_organization_profile_batch(
    organization,
    profile_ids,
    *,
    notify=False,
    activation_profile_ids=(),
):
    profile_ids = sorted(set(profile_ids))
    activation_profile_ids = set(activation_profile_ids)
    if not profile_ids:
        return {}
    room = (
        Room.objects.select_for_update()
        .filter(
            organization_id=organization.id,
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.ORGANIZATION,
        )
        .first()
    )
    if room is None:
        return {}

    roles = _organization_roles_for_ids(organization, profile_ids)
    memberships = {
        membership.user_id: membership
        for membership in UserRoom.objects.select_for_update()
        .filter(room=room, user_id__in=profile_ids)
        .order_by("id")
    }
    banned_ids = set(
        RoomBan.objects.filter(
            room=room,
            target_id__in=profile_ids,
            revoked_at__isnull=True,
        ).values_list("target_id", flat=True)
    )
    room_has_active_member = UserRoom.objects.filter(
        room=room,
        state=UserRoom.State.ACTIVE,
    ).exists()
    now = timezone.now()
    updates = []
    creates = []
    revoked_profile_ids = []
    changed_profile_ids = []
    bootstrap_assigned = room_has_active_member

    for profile_id in profile_ids:
        membership = memberships.get(profile_id)
        original_role = membership.role if membership else None
        original_values = (
            (
                membership.state,
                membership.role,
                membership.manual_role,
                membership.synced_role,
                membership.activated_at,
                membership.deactivated_at,
                membership.last_read_message_id,
                membership.unread_count,
                membership.is_hidden,
                membership.hidden_at,
            )
            if membership
            else None
        )
        synced_role = roles.get(profile_id)
        if synced_role is None:
            if membership and membership.state != UserRoom.State.INELIGIBLE:
                if membership.state == UserRoom.State.ACTIVE:
                    revoked_profile_ids.append(profile_id)
                membership.state = UserRoom.State.INELIGIBLE
                membership.role = None
                membership.manual_role = None
                membership.synced_role = None
                membership.deactivated_at = now
                membership.is_hidden = False
                membership.hidden_at = None
                updates.append(membership)
            continue
        if profile_id in banned_ids:
            if membership and membership.state == UserRoom.State.ACTIVE:
                membership.state = UserRoom.State.REMOVED
                membership.role = None
                membership.manual_role = None
                membership.synced_role = None
                membership.deactivated_at = now
                membership.is_hidden = False
                membership.hidden_at = None
                updates.append(membership)
                revoked_profile_ids.append(profile_id)
            continue
        if profile_id not in activation_profile_ids and (
            membership is None or membership.state != UserRoom.State.ACTIVE
        ):
            # Organization membership grants eligibility, but only an explicit
            # join creates or reactivates chat membership.
            continue

        activating = membership is None or membership.state != UserRoom.State.ACTIVE
        bootstrap = activating and not bootstrap_assigned
        if bootstrap:
            bootstrap_assigned = True
        manual_role = (
            UserRoom.Role.ADMIN
            if bootstrap and synced_role != UserRoom.Role.ADMIN
            else UserRoom.Role.MEMBER
        )
        if membership is None:
            membership = UserRoom(
                room=room,
                user_id=profile_id,
                state=UserRoom.State.ACTIVE,
                manual_role=manual_role,
                synced_role=synced_role,
                role=highest_role(manual_role, synced_role),
                activated_at=now,
                last_read_message_id=room.last_msg_id,
            )
            memberships[profile_id] = membership
            creates.append(membership)
            changed_profile_ids.append(profile_id)
            continue

        membership.state = UserRoom.State.ACTIVE
        if activating:
            membership.manual_role = manual_role
            membership.activated_at = now
            membership.last_read_message_id = room.last_msg_id
            membership.unread_count = 0
            membership.is_hidden = False
            membership.hidden_at = None
        membership.synced_role = synced_role
        membership.role = highest_role(membership.manual_role, synced_role)
        membership.deactivated_at = None
        if activating or membership.role != original_role:
            changed_profile_ids.append(profile_id)
        current_values = (
            membership.state,
            membership.role,
            membership.manual_role,
            membership.synced_role,
            membership.activated_at,
            membership.deactivated_at,
            membership.last_read_message_id,
            membership.unread_count,
            membership.is_hidden,
            membership.hidden_at,
        )
        if current_values != original_values:
            updates.append(membership)

    if creates:
        UserRoom.objects.bulk_create(creates, batch_size=SYNC_BATCH_SIZE)
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
            batch_size=SYNC_BATCH_SIZE,
        )
    if revoked_profile_ids:
        transaction.on_commit(
            lambda: revoke_room_subscriptions_many(revoked_profile_ids, room.id)
        )
    if notify:
        if changed_profile_ids:
            transaction.on_commit(
                lambda: broadcast_personal_events(
                    changed_profile_ids,
                    {"type": "room_membership_changed", "room": room.id},
                )
            )
        if changed_profile_ids or revoked_profile_ids:
            transaction.on_commit(
                lambda: broadcast_room_event(
                    room.id,
                    {"type": "membership_sync", "room": room.id},
                )
            )
    return memberships


def sync_organization_profiles(
    organization,
    profile_ids,
    *,
    notify=False,
    activation_profile_ids=(),
):
    memberships = {}
    activation_profile_ids = set(activation_profile_ids)
    for profile_id_batch in _batched(sorted(set(profile_ids))):
        memberships.update(
            _sync_organization_profile_batch(
                organization,
                profile_id_batch,
                notify=notify,
                activation_profile_ids=(
                    activation_profile_ids.intersection(profile_id_batch)
                ),
            )
        )
    return memberships


def sync_organization_profile(
    organization,
    profile_id,
    *,
    notify=False,
    activate=False,
):
    return sync_organization_profiles(
        organization,
        [profile_id],
        notify=notify,
        activation_profile_ids=([profile_id] if activate else ()),
    ).get(profile_id)


def _room_user_ids(room):
    profile_ids = set()
    last_membership_id = 0
    while True:
        membership_rows = list(
            UserRoom.objects.filter(
                room=room,
                id__gt=last_membership_id,
            )
            .order_by("id")
            .values_list("id", "user_id")[:SYNC_BATCH_SIZE]
        )
        if not membership_rows:
            return profile_ids
        profile_ids.update(user_id for _, user_id in membership_rows)
        last_membership_id = membership_rows[-1][0]


def _active_banned_user_ids(room):
    profile_ids = set()
    last_ban_id = 0
    while True:
        rows = list(
            RoomBan.objects.filter(
                room=room,
                revoked_at__isnull=True,
                id__gt=last_ban_id,
            )
            .order_by("id")
            .values_list("id", "target_id")[:SYNC_BATCH_SIZE]
        )
        if not rows:
            return profile_ids
        profile_ids.update(profile_id for _, profile_id in rows)
        last_ban_id = rows[-1][0]


def audit_organization_channel(organization):
    """Return reconciliation counts without mutating room or membership state."""
    room = Room.objects.filter(
        organization_id=organization.id,
        room_type=Room.Type.CHANNEL,
        channel_kind=Room.ChannelKind.ORGANIZATION,
    ).first()
    if room is None:
        return {
            "missing": 0,
            "extra": 0,
            "wrong_roles": 0,
            "name_drift": False,
        }
    eligible_ids = organization_user_ids(organization)
    banned_ids = _active_banned_user_ids(room)
    memberships = {}
    last_membership_id = 0
    while True:
        rows = list(
            UserRoom.objects.filter(room=room, id__gt=last_membership_id).order_by(
                "id"
            )[:SYNC_BATCH_SIZE]
        )
        if not rows:
            break
        memberships.update({membership.user_id: membership for membership in rows})
        last_membership_id = rows[-1].id

    expected_ids = eligible_ids - banned_ids
    active_ids = {
        profile_id
        for profile_id, membership in memberships.items()
        if membership.state == UserRoom.State.ACTIVE
    }
    extra_ids = active_ids - expected_ids
    wrong_roles = 0
    for profile_ids in _batched(sorted(active_ids & expected_ids)):
        synced_roles = _organization_roles_for_ids(organization, profile_ids)
        for profile_id in profile_ids:
            membership = memberships[profile_id]
            synced_role = synced_roles.get(profile_id)
            expected_role = highest_role(membership.manual_role, synced_role)
            if (
                membership.synced_role != synced_role
                or membership.role != expected_role
            ):
                wrong_roles += 1
    return {
        # Eligible users without a membership are intentionally not enrolled.
        "missing": 0,
        "extra": len(extra_ids),
        "wrong_roles": wrong_roles,
        "name_drift": room.name != organization.name,
    }


def sync_organization_channel(organization):
    room = Room.objects.filter(
        organization_id=organization.id,
        room_type=Room.Type.CHANNEL,
        channel_kind=Room.ChannelKind.ORGANIZATION,
    ).first()
    if room is None:
        return None

    # Reconcile only existing memberships. Organization eligibility must never
    # enroll somebody in chat without an explicit join.
    sync_organization_profiles(organization, _room_user_ids(room))
    return room
