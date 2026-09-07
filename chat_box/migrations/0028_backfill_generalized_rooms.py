from django.db import migrations, transaction
from django.db.models import Q
from django.utils import timezone

BACKFILL_BATCH_SIZE = 500


def batched(values, size=BACKFILL_BATCH_SIZE):
    batch = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def move_room_messages(Message, source_room_ids, target_room_id):
    """Move a legacy room history without issuing an unbounded UPDATE."""
    for source_room_id_batch in batched(source_room_ids):
        while True:
            message_ids = list(
                Message.objects.filter(room_id__in=source_room_id_batch)
                .order_by("id")
                .values_list("id", flat=True)[:BACKFILL_BATCH_SIZE]
            )
            if not message_ids:
                break
            Message.objects.filter(id__in=message_ids).update(room_id=target_room_id)


def merge_legacy_room_memberships(UserRoom, room_ids, target_room_id):
    """Merge the per-room unread state for one canonical legacy DM pair."""
    memberships_by_user = {}
    for room_id_batch in batched(room_ids):
        for membership in UserRoom.objects.filter(room_id__in=room_id_batch).order_by(
            "user_id", "id"
        ):
            memberships_by_user.setdefault(membership.user_id, []).append(membership)

    updates = []
    duplicate_membership_ids = []
    for memberships in memberships_by_user.values():
        keeper = next(
            membership
            for membership in memberships
            if membership.room_id == target_room_id
        )
        keeper.unread_count = sum(
            max(0, membership.unread_count) for membership in memberships
        )
        keeper.last_seen = max(membership.last_seen for membership in memberships)
        keeper.joined_at = min(membership.joined_at for membership in memberships)
        keeper.activated_at = min(membership.activated_at for membership in memberships)
        keeper.is_hidden = any(membership.is_hidden for membership in memberships)
        hidden_times = [
            membership.hidden_at
            for membership in memberships
            if membership.hidden_at is not None
        ]
        keeper.hidden_at = max(hidden_times) if hidden_times else None
        updates.append(keeper)
        duplicate_membership_ids.extend(
            membership.id for membership in memberships if membership.id != keeper.id
        )

    if updates:
        UserRoom.objects.bulk_update(
            updates,
            [
                "unread_count",
                "last_seen",
                "joined_at",
                "activated_at",
                "is_hidden",
                "hidden_at",
            ],
            batch_size=BACKFILL_BATCH_SIZE,
        )
    for membership_ids in batched(duplicate_membership_ids):
        UserRoom.objects.filter(id__in=membership_ids).delete()


def reconcile_legacy_direct_rooms(Message, Room, RoomRedirect, UserRoom, lobby_id, now):
    """Merge exact legacy DMs without conflating ambiguous one-member history."""
    first_room_by_pair = {}
    duplicate_rooms_by_pair = {}
    empty_room_ids = []
    last_room_id = 0
    while True:
        room_ids = list(
            Room.objects.exclude(pk=lobby_id)
            .filter(pk__gt=last_room_id, archived_at__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)[:BACKFILL_BATCH_SIZE]
        )
        if not room_ids:
            break
        last_room_id = room_ids[-1]
        room_to_members = {room_id: [] for room_id in room_ids}
        for room_id, user_id in (
            UserRoom.objects.filter(room_id__in=room_ids)
            .order_by("room_id", "user_id")
            .values_list("room_id", "user_id")
        ):
            room_to_members[room_id].append(user_id)
        for room_id in room_ids:
            members = tuple(sorted(set(room_to_members[room_id])))
            if not members:
                empty_room_ids.append(room_id)
                continue
            if len(members) > 2:
                raise RuntimeError(
                    "Legacy chat room %(room)s has %(count)s distinct members; "
                    "run chat_room_preflight before migrating."
                    % {"room": room_id, "count": len(members)}
                )
            pair = (members[0], members[-1])
            first_room_id = first_room_by_pair.get(pair)
            if first_room_id is None:
                first_room_by_pair[pair] = room_id
            else:
                duplicate_rooms_by_pair.setdefault(pair, [first_room_id]).append(
                    room_id
                )

    for room_ids in batched(empty_room_ids):
        rooms_with_messages = set(
            Message.objects.filter(room_id__in=room_ids)
            .values_list("room_id", flat=True)
            .distinct()
        )
        if rooms_with_messages:
            raise RuntimeError(
                "Legacy chat rooms without members still contain messages: %s; "
                "run chat_room_preflight before migrating."
                % sorted(rooms_with_messages)[:10]
            )
        Room.objects.filter(id__in=room_ids).update(
            room_type="direct",
            channel_kind=None,
            name=None,
            direct_user_low_id=None,
            direct_user_high_id=None,
            last_msg_id=None,
            last_activity_at=None,
            archived_at=now,
            archive_reason="empty_legacy",
        )

    duplicate_room_ids = [
        room_id for room_ids in duplicate_rooms_by_pair.values() for room_id in room_ids
    ]
    last_message_by_room = {}
    for room_id_batch in batched(duplicate_room_ids):
        last_message_by_room.update(
            Room.objects.filter(id__in=room_id_batch).values_list("id", "last_msg_id")
        )

    for pair, room_ids in duplicate_rooms_by_pair.items():
        # last_msg_id was the legacy room-list activity cursor. Retain the room
        # users were most likely visiting; use its primary key as a stable tie-break.
        target_room_id = None
        target_key = None
        for room_id in room_ids:
            candidate_key = (last_message_by_room.get(room_id) or -1, room_id)
            if target_key is None or candidate_key > target_key:
                target_room_id = room_id
                target_key = candidate_key
        source_room_ids = [room_id for room_id in room_ids if room_id != target_room_id]
        if pair[0] == pair[1]:
            # A one-member legacy room may be either Saved Messages or a DM whose
            # other profile was cascade-deleted. There is no surviving discriminator.
            # Keep every history intact and make only the most active room the
            # canonical self-DM; expose the others through Archived rooms.
            for source_room_id_batch in batched(source_room_ids):
                Room.objects.filter(id__in=source_room_id_batch).update(
                    room_type="direct",
                    channel_kind=None,
                    name=None,
                    direct_user_low_id=None,
                    direct_user_high_id=None,
                    archived_at=now,
                    archive_reason="ambiguous_legacy",
                )
            continue
        # The migration itself is non-atomic so each SQL statement stays bounded,
        # but a single duplicate group must be all-or-nothing. Otherwise a retry
        # after updating the keeper but before deleting duplicate memberships
        # could add the same unread counters twice.
        with transaction.atomic():
            move_room_messages(Message, source_room_ids, target_room_id)
            merge_legacy_room_memberships(UserRoom, room_ids, target_room_id)
            for source_room_id_batch in batched(source_room_ids):
                RoomRedirect.objects.bulk_create(
                    [
                        RoomRedirect(
                            old_room_id=source_room_id,
                            canonical_room_id=target_room_id,
                        )
                        for source_room_id in source_room_id_batch
                    ]
                )
                Room.objects.filter(id__in=source_room_id_batch).delete()


def backfill_direct_message_cursors(Message, UserRoom, lobby_id):
    """Convert legacy unread counters to exact message-ID cursors.

    Legacy direct messages incremented ``unread_count`` only for messages from the
    other participant. A timestamp cursor is therefore not exact when somebody
    sends a message without first reading messages they received. Two keyset
    passes preserve the counter exactly while retaining only membership-sized
    state in memory and keeping every SQL result bounded.
    """
    memberships_by_room = {}
    membership_by_id = {}
    last_membership_id = 0
    while True:
        membership_batch = list(
            UserRoom.objects.exclude(room_id=lobby_id)
            .filter(id__gt=last_membership_id)
            .order_by("id")
            .values_list("id", "room_id", "user_id", "unread_count")[
                :BACKFILL_BATCH_SIZE
            ]
        )
        if not membership_batch:
            break
        last_membership_id = membership_batch[-1][0]
        for membership_id, room_id, user_id, unread_count in membership_batch:
            membership = {
                "id": membership_id,
                "room_id": room_id,
                "user_id": user_id,
                "unread_count": max(0, unread_count),
                "other_count": 0,
                "seen_other_count": 0,
            }
            membership_by_id[membership_id] = membership
            memberships_by_room.setdefault(room_id, []).append(membership)

    room_tail_ids = {}
    last_message_id = 0
    while True:
        message_batch = list(
            Message.objects.exclude(room_id=lobby_id)
            .filter(id__gt=last_message_id, hidden=False, kind="user")
            .order_by("id")
            .values_list("id", "room_id", "author_id")[:BACKFILL_BATCH_SIZE]
        )
        if not message_batch:
            break
        last_message_id = message_batch[-1][0]
        for message_id, room_id, author_id in message_batch:
            room_tail_ids[room_id] = message_id
            for membership in memberships_by_room.get(room_id, ()):
                if author_id != membership["user_id"]:
                    membership["other_count"] += 1

    cursor_by_membership_id = {}
    target_ordinal_by_membership_id = {}
    for membership_id, membership in membership_by_id.items():
        unread_count = min(membership["unread_count"], membership["other_count"])
        if unread_count == 0:
            cursor_by_membership_id[membership_id] = room_tail_ids.get(
                membership["room_id"]
            )
        else:
            target_ordinal_by_membership_id[membership_id] = (
                membership["other_count"] - unread_count + 1
            )

    if target_ordinal_by_membership_id:
        last_message_id = 0
        while True:
            message_batch = list(
                Message.objects.exclude(room_id=lobby_id)
                .filter(id__gt=last_message_id, hidden=False, kind="user")
                .order_by("id")
                .values_list("id", "room_id", "author_id")[:BACKFILL_BATCH_SIZE]
            )
            if not message_batch:
                break
            last_message_id = message_batch[-1][0]
            for message_id, room_id, author_id in message_batch:
                for membership in memberships_by_room.get(room_id, ()):
                    membership_id = membership["id"]
                    if membership_id not in target_ordinal_by_membership_id:
                        continue
                    if author_id == membership["user_id"]:
                        continue
                    membership["seen_other_count"] += 1
                    if (
                        membership["seen_other_count"]
                        == target_ordinal_by_membership_id[membership_id]
                    ):
                        # A cursor need not reference a message. Position it just
                        # before the first unread message to preserve that message.
                        cursor_by_membership_id[membership_id] = max(0, message_id - 1)

    for membership_ids in batched(cursor_by_membership_id):
        memberships = UserRoom.objects.in_bulk(membership_ids)
        updates = []
        for membership_id in membership_ids:
            membership = memberships[membership_id]
            membership.last_read_message_id = cursor_by_membership_id[membership_id]
            updates.append(membership)
        UserRoom.objects.bulk_update(
            updates,
            ["last_read_message_id"],
            batch_size=BACKFILL_BATCH_SIZE,
        )
    return room_tail_ids


def update_direct_room_tails(Message, Room, lobby_id, room_tail_ids):
    """Recalculate previews after duplicate histories have been combined."""
    last_room_id = 0
    while True:
        rooms = list(
            Room.objects.exclude(pk=lobby_id)
            .filter(id__gt=last_room_id, archived_at__isnull=True)
            .order_by("id")[:BACKFILL_BATCH_SIZE]
        )
        if not rooms:
            return
        last_room_id = rooms[-1].id
        tail_ids = {
            room_tail_ids[room.id] for room in rooms if room.id in room_tail_ids
        }
        tail_times = dict(
            Message.objects.filter(id__in=tail_ids).values_list("id", "time")
        )
        for room in rooms:
            room.last_msg_id = room_tail_ids.get(room.id)
            room.last_activity_at = tail_times.get(room.last_msg_id)
        Room.objects.bulk_update(
            rooms,
            ["last_msg_id", "last_activity_at"],
            batch_size=BACKFILL_BATCH_SIZE,
        )


def backfill_lobby_cursors(Message, UserRoom, lobby_id):
    """Map Lobby timestamps to ID cursors with one message batch resident."""
    message_batch = []
    message_index = 0
    message_cursor_time = None
    message_cursor_id = 0
    latest_visible_id = None

    def next_message():
        nonlocal message_batch, message_index, message_cursor_time, message_cursor_id
        if message_index >= len(message_batch):
            messages = Message.objects.filter(room_id=lobby_id, hidden=False)
            if message_cursor_time is not None:
                messages = messages.filter(
                    Q(time__gt=message_cursor_time)
                    | Q(time=message_cursor_time, id__gt=message_cursor_id)
                )
            message_batch = list(
                messages.order_by("time", "id").values_list("time", "id")[
                    :BACKFILL_BATCH_SIZE
                ]
            )
            message_index = 0
            if not message_batch:
                return None
            message_cursor_time, message_cursor_id = message_batch[-1]
        return message_batch[message_index]

    lookahead = next_message()
    last_seen = None
    last_seen_membership_id = 0
    while True:
        lobby_memberships = UserRoom.objects.filter(room_id=lobby_id)
        if last_seen is not None:
            lobby_memberships = lobby_memberships.filter(
                Q(last_seen__gt=last_seen)
                | Q(last_seen=last_seen, id__gt=last_seen_membership_id)
            )
        membership_batch = list(
            lobby_memberships.order_by("last_seen", "id")[:BACKFILL_BATCH_SIZE]
        )
        if not membership_batch:
            break
        for membership in membership_batch:
            while lookahead is not None and lookahead[0] <= membership.last_seen:
                latest_visible_id = max(latest_visible_id or 0, lookahead[1])
                message_index += 1
                lookahead = next_message()
            membership.last_read_message_id = latest_visible_id
        UserRoom.objects.bulk_update(
            membership_batch,
            ["last_read_message_id"],
            batch_size=BACKFILL_BATCH_SIZE,
        )
        last_seen = membership_batch[-1].last_seen
        last_seen_membership_id = membership_batch[-1].id


def backfill_generalized_rooms(apps, schema_editor):
    Message = apps.get_model("chat_box", "Message")
    Room = apps.get_model("chat_box", "Room")
    RoomRedirect = apps.get_model("chat_box", "RoomRedirect")
    UserRoom = apps.get_model("chat_box", "UserRoom")
    Profile = apps.get_model("judge", "Profile")

    now = timezone.now()
    orphan_membership_ids = []
    orphan_room_ids = set()
    last_membership_id = 0
    while True:
        membership_users = list(
            UserRoom.objects.filter(id__gt=last_membership_id)
            .order_by("id")
            .values_list("id", "user_id", "room_id")[:BACKFILL_BATCH_SIZE]
        )
        if not membership_users:
            break
        last_membership_id = membership_users[-1][0]
        user_ids = {user_id for _, user_id, _ in membership_users}
        existing_user_ids = set(
            Profile.objects.filter(id__in=user_ids).values_list("id", flat=True)
        )
        for membership_id, user_id, room_id in membership_users:
            if user_id not in existing_user_ids:
                orphan_membership_ids.append(membership_id)
                if room_id is not None:
                    orphan_room_ids.add(room_id)
    for room_ids in batched(orphan_room_ids):
        Room.objects.filter(id__in=room_ids).update(
            archived_at=now,
            archive_reason="profile_deleted",
        )
    for membership_ids in batched(orphan_membership_ids):
        UserRoom.objects.filter(id__in=membership_ids).delete()

    lobby, _ = Room.objects.get_or_create(
        singleton_key="lobby",
        defaults={
            "room_type": "channel",
            "channel_kind": "lobby",
            "name": "Lobby",
            "created_at": now,
        },
    )
    Room.objects.filter(pk=lobby.pk).update(
        room_type="channel",
        channel_kind="lobby",
        name="Lobby",
        archived_at=None,
        archive_reason="",
    )
    while True:
        message_ids = list(
            Message.objects.filter(room__isnull=True).values_list("id", flat=True)[
                :BACKFILL_BATCH_SIZE
            ]
        )
        if not message_ids:
            break
        Message.objects.filter(id__in=message_ids).update(room_id=lobby.pk)

    # Nullable room IDs allowed duplicate legacy Lobby trackers. Keep the newest
    # tracker per profile and merge its conservative unread state before assigning
    # the persisted Lobby FK.
    keeper_by_user = {}
    duplicate_ids = []
    last_tracker_id = 0
    while True:
        trackers = list(
            UserRoom.objects.filter(room__isnull=True, id__gt=last_tracker_id).order_by(
                "id"
            )[:BACKFILL_BATCH_SIZE]
        )
        if not trackers:
            break
        last_tracker_id = trackers[-1].id
        for tracker in trackers:
            keeper = keeper_by_user.get(tracker.user_id)
            if keeper is None:
                keeper_by_user[tracker.user_id] = tracker
                continue
            if (tracker.last_seen, tracker.id) > (keeper.last_seen, keeper.id):
                tracker.unread_count = max(tracker.unread_count, keeper.unread_count)
                duplicate_ids.append(keeper.id)
                keeper_by_user[tracker.user_id] = tracker
            else:
                keeper.unread_count = max(keeper.unread_count, tracker.unread_count)
                duplicate_ids.append(tracker.id)
    for keepers in batched(keeper_by_user.values()):
        UserRoom.objects.bulk_update(
            keepers,
            ["unread_count"],
            batch_size=BACKFILL_BATCH_SIZE,
        )
    for membership_ids in batched(duplicate_ids):
        UserRoom.objects.filter(id__in=membership_ids).delete()

    while True:
        membership_ids = list(
            UserRoom.objects.filter(room__isnull=True)
            .order_by("id")
            .values_list("id", flat=True)[:BACKFILL_BATCH_SIZE]
        )
        if not membership_ids:
            break
        UserRoom.objects.filter(id__in=membership_ids).update(
            room_id=lobby.pk,
            state="active",
            role="member",
            manual_role="member",
            synced_role="member",
            deactivated_at=None,
        )

    reconcile_legacy_direct_rooms(Message, Room, RoomRedirect, UserRoom, lobby.pk, now)

    # Derive canonical unordered pairs after duplicate histories have been merged.
    # Archived legacy shells intentionally have no canonical participants.
    pair_to_room = {}
    last_room_id = 0
    found_rooms = False
    while True:
        room_ids = list(
            Room.objects.exclude(pk=lobby.pk)
            .filter(pk__gt=last_room_id)
            .order_by("pk")
            .values_list("pk", flat=True)[:BACKFILL_BATCH_SIZE]
        )
        if not room_ids:
            break
        found_rooms = True
        last_room_id = room_ids[-1]
        room_to_members = {room_id: [] for room_id in room_ids}
        for room_id, user_id in (
            UserRoom.objects.filter(room_id__in=room_ids)
            .order_by("room_id", "user_id")
            .values_list("room_id", "user_id")
        ):
            room_to_members[room_id].append(user_id)
        rooms = Room.objects.in_bulk(room_ids)
        message_times = dict(
            Message.objects.filter(
                id__in={room.last_msg_id for room in rooms.values() if room.last_msg_id}
            ).values_list("id", "time")
        )
        room_updates = []
        for room_id in room_ids:
            room = rooms[room_id]
            members = sorted(set(room_to_members.get(room_id, [])))
            if room.archived_at is not None:
                room.room_type = "direct"
                room.channel_kind = None
                room.name = None
                room.direct_user_low_id = None
                room.direct_user_high_id = None
                if room.archive_reason != "ambiguous_legacy":
                    room.last_msg_id = None
                    room.last_activity_at = None
            elif len(members) not in (1, 2):
                raise RuntimeError(
                    "Legacy chat room %(room)s has %(count)s distinct members; "
                    "run chat_room_preflight before migrating."
                    % {"room": room_id, "count": len(members)}
                )
            else:
                low_id = members[0]
                # The legacy application deliberately represented Saved Messages
                # as a one-membership room and resolved any such room as a self-DM.
                # Preserve that established identity as the canonical (user, user)
                # pair. Verified deleted-profile rooms were classified above.
                high_id = members[-1]
                room.room_type = "direct"
                room.channel_kind = None
                room.name = None
                room.direct_user_low_id = low_id
                room.direct_user_high_id = high_id
                pair = (low_id, high_id)
                if pair in pair_to_room:
                    raise RuntimeError(
                        "Legacy chat rooms %(first)s and %(second)s duplicate "
                        "profile pair %(low)s/%(high)s; run "
                        "chat_room_preflight before migrating."
                        % {
                            "first": pair_to_room[pair],
                            "second": room_id,
                            "low": low_id,
                            "high": high_id,
                        }
                    )
                pair_to_room[pair] = room_id
            room.last_activity_at = message_times.get(room.last_msg_id)
            room_updates.append(room)
        Room.objects.bulk_update(
            room_updates,
            [
                "room_type",
                "channel_kind",
                "name",
                "direct_user_low",
                "direct_user_high",
                "archived_at",
                "archive_reason",
                "last_msg_id",
                "last_activity_at",
            ],
            batch_size=BACKFILL_BATCH_SIZE,
        )

    if found_rooms:
        last_membership_id = 0
        while True:
            membership_ids = list(
                UserRoom.objects.exclude(room_id=lobby.pk)
                .filter(id__gt=last_membership_id)
                .order_by("id")
                .values_list("id", flat=True)[:BACKFILL_BATCH_SIZE]
            )
            if not membership_ids:
                break
            last_membership_id = membership_ids[-1]
            UserRoom.objects.filter(id__in=membership_ids).update(
                state="active",
                role=None,
                manual_role=None,
                synced_role=None,
                deactivated_at=None,
            )

        room_tail_ids = backfill_direct_message_cursors(Message, UserRoom, lobby.pk)
        update_direct_room_tails(Message, Room, lobby.pk, room_tail_ids)

    lobby_tail = (
        Message.objects.filter(room_id=lobby.pk, hidden=False, kind="user")
        .order_by("-id")
        .values("id", "time")
        .first()
    )
    Room.objects.filter(pk=lobby.pk).update(
        last_msg_id=lobby_tail["id"] if lobby_tail else None,
        last_activity_at=lobby_tail["time"] if lobby_tail else None,
    )

    lobby.refresh_from_db()
    backfill_lobby_cursors(Message, UserRoom, lobby.pk)

    tail_id = lobby.last_msg_id
    pending_memberships = []
    last_profile_id = 0
    while True:
        profile_ids = list(
            Profile.objects.filter(id__gt=last_profile_id)
            .order_by("id")
            .values_list("id", flat=True)[:BACKFILL_BATCH_SIZE]
        )
        if not profile_ids:
            break
        last_profile_id = profile_ids[-1]
        existing_lobby_users = set(
            UserRoom.objects.filter(
                room_id=lobby.pk,
                user_id__in=profile_ids,
            ).values_list("user_id", flat=True)
        )
        for profile_id in profile_ids:
            if profile_id in existing_lobby_users:
                continue
            pending_memberships.append(
                UserRoom(
                    user_id=profile_id,
                    room_id=lobby.pk,
                    last_seen=now,
                    state="active",
                    role="member",
                    manual_role="member",
                    synced_role="member",
                    joined_at=now,
                    activated_at=now,
                    last_read_message_id=tail_id,
                )
            )
        if pending_memberships:
            UserRoom.objects.bulk_create(
                pending_memberships,
                ignore_conflicts=True,
                batch_size=BACKFILL_BATCH_SIZE,
            )
            pending_memberships = []

    last_profile_id = 0
    while True:
        profile_ids = list(
            Profile.objects.filter(id__gt=last_profile_id)
            .order_by("id")
            .values_list("id", flat=True)[:BACKFILL_BATCH_SIZE]
        )
        if not profile_ids:
            break
        last_profile_id = profile_ids[-1]
        moderator_profile_ids = list(
            Profile.objects.filter(id__in=profile_ids)
            .filter(
                Q(
                    user__user_permissions__content_type__app_label="judge",
                    user__user_permissions__codename="change_comment",
                )
                | Q(
                    user__groups__permissions__content_type__app_label="judge",
                    user__groups__permissions__codename="change_comment",
                )
            )
            .filter(user__is_superuser=False)
            .distinct()
            .values_list("id", flat=True)
        )
        if moderator_profile_ids:
            UserRoom.objects.filter(
                room_id=lobby.pk,
                user_id__in=moderator_profile_ids,
            ).update(
                role="moderator",
                manual_role="moderator",
                synced_role="moderator",
            )
        superuser_profile_ids = list(
            Profile.objects.filter(
                id__in=profile_ids,
                user__is_superuser=True,
            ).values_list("id", flat=True)
        )
        if superuser_profile_ids:
            UserRoom.objects.filter(
                room_id=lobby.pk,
                user_id__in=superuser_profile_ids,
            ).update(role="admin", manual_role=None, synced_role="admin")


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("chat_box", "0027_prepare_generalized_rooms"),
    ]

    operations = [
        migrations.RunPython(
            backfill_generalized_rooms,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
