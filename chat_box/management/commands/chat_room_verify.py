import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Count, Q

from judge.models import Profile

from chat_box.models import Message, MessageReaction, Room, RoomRedirect, UserRoom

VERIFY_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Read-only verification for the completed generalized chat migration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--snapshot",
            help="Compare against a snapshot written by chat_room_preflight.",
        )

    def handle(self, *args, **options):
        statement_timings = []

        def time_statement(execute, sql, params, many, context):
            started = time.monotonic()
            try:
                return execute(sql, params, many, context)
            finally:
                statement_timings.append(
                    (time.monotonic() - started, sql.split(None, 1)[0])
                )

        with connection.execute_wrapper(time_statement):
            errors, counts = self._verify()
            if options["snapshot"]:
                self._verify_snapshot(options["snapshot"], errors)

        slowest_seconds, slowest_kind = max(
            statement_timings,
            default=(0.0, "none"),
        )
        self.stdout.write("Generalized chat-room verification")
        for label, count in counts:
            self.stdout.write("  %s: %s" % (label, count))
        self.stdout.write(
            "  Slowest SQL statement: %.3fs (%s)" % (slowest_seconds, slowest_kind)
        )
        if slowest_seconds >= 8:
            errors.append("A verification query exceeded the 8-second server limit.")
        if errors:
            for error in errors:
                self.stderr.write(self.style.ERROR("  %s" % error))
            raise CommandError("Generalized chat-room verification failed.")
        self.stdout.write(self.style.SUCCESS("Verification passed."))

    def _verify_snapshot(self, snapshot_path, errors):
        try:
            snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CommandError("Cannot read chat migration snapshot: %s" % error)
        if snapshot.get("version") != 1:
            raise CommandError("Unsupported chat migration snapshot version.")
        lobby = Room.objects.filter(singleton_key="lobby").first()
        actual = {
            "profiles": Profile.objects.count(),
            "messages": Message.objects.count(),
            "message_reactions": MessageReaction.objects.count(),
            "replies": Message.objects.filter(reply_to__isnull=False).count(),
            "lobby_messages": (
                Message.objects.filter(room=lobby).count() if lobby else None
            ),
        }
        for key, expected_value in snapshot.items():
            if key == "version":
                continue
            actual_value = actual.get(key)
            if actual_value != expected_value:
                errors.append(
                    "Snapshot mismatch for %s: expected %s, found %s."
                    % (key, expected_value, actual_value)
                )

    def _verify(self):
        errors = []
        counts = []
        required_tables = {
            Room._meta.db_table,
            RoomRedirect._meta.db_table,
            UserRoom._meta.db_table,
            Message._meta.db_table,
        }
        missing_tables = required_tables.difference(
            connection.introspection.table_names()
        )
        if missing_tables:
            raise CommandError(
                "Generalized chat schema is incomplete; missing tables: %s"
                % ", ".join(sorted(missing_tables))
            )

        profile_count = Profile.objects.count()
        lobby_ids = list(
            Room.objects.filter(
                room_type=Room.Type.CHANNEL,
                channel_kind=Room.ChannelKind.LOBBY,
                singleton_key="lobby",
                archived_at__isnull=True,
            ).values_list("id", flat=True)[:2]
        )
        if len(lobby_ids) != 1:
            errors.append("Expected exactly one active persisted Lobby.")
            lobby_id = None
        else:
            lobby_id = lobby_ids[0]

        null_message_count = Message.objects.filter(room__isnull=True).count()
        null_membership_count = UserRoom.objects.filter(room__isnull=True).count()
        counts.extend(
            [
                ("Profiles", profile_count),
                ("Messages", Message.objects.count()),
                ("Memberships", UserRoom.objects.count()),
                ("Legacy roomless messages", null_message_count),
                ("Legacy roomless memberships", null_membership_count),
                ("Legacy redirects", RoomRedirect.objects.count()),
            ]
        )
        if null_message_count:
            errors.append("Roomless legacy messages remain.")
        if null_membership_count:
            errors.append("Roomless legacy memberships remain.")

        if lobby_id is not None:
            lobby_memberships = UserRoom.objects.filter(room_id=lobby_id)
            lobby_membership_count = lobby_memberships.count()
            active_lobby_membership_count = lobby_memberships.filter(
                state=UserRoom.State.ACTIVE
            ).count()
            counts.append(("Lobby memberships", lobby_membership_count))
            if (
                lobby_membership_count != profile_count
                or active_lobby_membership_count != profile_count
            ):
                errors.append("Lobby does not have one active membership per profile.")

        invalid_role_count = UserRoom.objects.filter(
            state=UserRoom.State.ACTIVE,
            room__room_type=Room.Type.DIRECT,
            role__isnull=False,
        ).count()
        invalid_role_count += UserRoom.objects.filter(
            state=UserRoom.State.ACTIVE,
            room__room_type__in=[Room.Type.GROUP, Room.Type.CHANNEL],
            role__isnull=True,
        ).count()
        invalid_role_count += (
            UserRoom.objects.exclude(state=UserRoom.State.ACTIVE)
            .filter(role__isnull=False)
            .count()
        )
        if invalid_role_count:
            errors.append(
                "Found %s memberships with an invalid role." % invalid_role_count
            )

        self._verify_direct_rooms(errors, counts)
        self._verify_managed_rooms(errors, counts)
        self._verify_redirects(errors)
        self._verify_replies(errors)
        self._verify_last_messages(errors)
        return errors, counts

    def _verify_direct_rooms(self, errors, counts):
        seen_pairs = {}
        invalid_rooms = []
        direct_room_count = 0
        last_room_id = 0
        while True:
            rooms = list(
                Room.objects.filter(
                    room_type=Room.Type.DIRECT,
                    archived_at__isnull=True,
                    id__gt=last_room_id,
                )
                .order_by("id")
                .values("id", "direct_user_low_id", "direct_user_high_id")[
                    :VERIFY_BATCH_SIZE
                ]
            )
            if not rooms:
                break
            last_room_id = rooms[-1]["id"]
            room_ids = [room["id"] for room in rooms]
            member_ids = {room_id: [] for room_id in room_ids}
            for room_id, user_id in UserRoom.objects.filter(
                room_id__in=room_ids,
                state=UserRoom.State.ACTIVE,
            ).values_list("room_id", "user_id"):
                member_ids[room_id].append(user_id)
            for room in rooms:
                direct_room_count += 1
                pair = (room["direct_user_low_id"], room["direct_user_high_id"])
                expected_members = sorted(set(pair)) if None not in pair else []
                actual_members = sorted(set(member_ids[room["id"]]))
                if (
                    pair[0] is None
                    or pair[0] > pair[1]
                    or actual_members != expected_members
                ):
                    invalid_rooms.append(room["id"])
                prior_room_id = seen_pairs.get(pair)
                if prior_room_id is not None:
                    invalid_rooms.extend([prior_room_id, room["id"]])
                else:
                    seen_pairs[pair] = room["id"]
        counts.append(("Active direct rooms", direct_room_count))
        if invalid_rooms:
            errors.append(
                "Invalid or duplicate active direct rooms: %s"
                % sorted(set(invalid_rooms))[:10]
            )

    def _verify_managed_rooms(self, errors, counts):
        invalid_admin_room_ids = []
        oversized_group_ids = []
        managed_room_count = 0
        last_room_id = 0
        while True:
            room_rows = list(
                Room.objects.filter(
                    Q(room_type=Room.Type.GROUP)
                    | Q(
                        room_type=Room.Type.CHANNEL,
                        channel_kind=Room.ChannelKind.CUSTOM,
                    ),
                    archived_at__isnull=True,
                    id__gt=last_room_id,
                )
                .order_by("id")
                .values_list("id", "room_type")[:VERIFY_BATCH_SIZE]
            )
            if not room_rows:
                break
            last_room_id = room_rows[-1][0]
            room_types = dict(room_rows)
            membership_counts = {
                row["room_id"]: row
                for row in UserRoom.objects.filter(
                    room_id__in=room_types,
                    state=UserRoom.State.ACTIVE,
                )
                .values("room_id")
                .annotate(
                    member_count=Count("id"),
                    admin_count=Count(
                        "id",
                        filter=Q(role=UserRoom.Role.ADMIN),
                    ),
                )
            }
            for room_id, room_type in room_rows:
                managed_room_count += 1
                room_counts = membership_counts.get(room_id, {})
                if not room_counts.get("admin_count", 0):
                    invalid_admin_room_ids.append(room_id)
                if (
                    room_type == Room.Type.GROUP
                    and room_counts.get("member_count", 0) > 50
                ):
                    oversized_group_ids.append(room_id)
        counts.append(("Active groups/custom channels", managed_room_count))
        if invalid_admin_room_ids:
            errors.append(
                "Groups/custom channels without an administrator: %s"
                % invalid_admin_room_ids[:10]
            )
        if oversized_group_ids:
            errors.append("Groups above 50 members: %s" % oversized_group_ids[:10])

    def _verify_redirects(self, errors):
        last_old_room_id = 0
        while True:
            redirects = list(
                RoomRedirect.objects.filter(old_room_id__gt=last_old_room_id)
                .order_by("old_room_id")
                .values_list("old_room_id", "canonical_room_id")[:VERIFY_BATCH_SIZE]
            )
            if not redirects:
                break
            last_old_room_id = redirects[-1][0]
            old_ids = [old_id for old_id, _ in redirects]
            canonical_ids = {canonical_id for _, canonical_id in redirects}
            if Room.objects.filter(id__in=old_ids).exists():
                errors.append("A legacy redirect shadows an existing room ID.")
            invalid_targets = Room.objects.filter(id__in=canonical_ids).exclude(
                room_type=Room.Type.DIRECT,
                archived_at__isnull=True,
            )
            if invalid_targets.exists():
                errors.append(
                    "A legacy redirect does not target an active direct room."
                )

    def _verify_replies(self, errors):
        last_message_id = 0
        cross_room_ids = []
        while True:
            replies = list(
                Message.objects.filter(
                    reply_to__isnull=False,
                    id__gt=last_message_id,
                )
                .order_by("id")
                .values_list("id", "room_id", "reply_to__room_id")[:VERIFY_BATCH_SIZE]
            )
            if not replies:
                break
            last_message_id = replies[-1][0]
            cross_room_ids.extend(
                message_id
                for message_id, room_id, parent_room_id in replies
                if room_id != parent_room_id
            )
            if len(cross_room_ids) >= 10:
                break
        if cross_room_ids:
            errors.append("Cross-room reply messages: %s" % cross_room_ids[:10])

    def _verify_last_messages(self, errors):
        last_room_id = 0
        invalid_room_ids = []
        while True:
            room_tails = list(
                Room.objects.filter(
                    last_msg_id__isnull=False,
                    id__gt=last_room_id,
                )
                .order_by("id")
                .values_list("id", "last_msg_id")[:VERIFY_BATCH_SIZE]
            )
            if not room_tails:
                break
            last_room_id = room_tails[-1][0]
            message_rooms = dict(
                Message.objects.filter(
                    id__in=[message_id for _, message_id in room_tails]
                ).values_list("id", "room_id")
            )
            invalid_room_ids.extend(
                room_id
                for room_id, message_id in room_tails
                if message_rooms.get(message_id) != room_id
            )
            if len(invalid_room_ids) >= 10:
                break
        if invalid_room_ids:
            errors.append(
                "Rooms with invalid last-message pointers: %s" % invalid_room_ids[:10]
            )
