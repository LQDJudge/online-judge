import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from chat_box.models import Message, MessageReaction, Room, UserRoom
from judge.models import Profile

PREFLIGHT_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Read-only preflight for the generalized chat-room backfill."

    def add_arguments(self, parser):
        parser.add_argument(
            "--require-online-ddl",
            action="store_true",
            help="Fail unless the database meets the production online-DDL baseline.",
        )
        parser.add_argument(
            "--snapshot",
            help="Write immutable pre-migration counts for chat_room_verify.",
        )

    def handle(self, *args, **options):
        self.require_online_ddl = options["require_online_ddl"]
        self.snapshot_path = options["snapshot"]
        statement_timings = []

        def time_statement(execute, sql, params, many, context):
            statement_started = time.monotonic()
            try:
                return execute(sql, params, many, context)
            finally:
                statement_timings.append(
                    (time.monotonic() - statement_started, sql.split(None, 1)[0])
                )

        with connection.execute_wrapper(time_statement):
            result = self._run_preflight()

        if self.snapshot_path:
            try:
                Path(self.snapshot_path).write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as error:
                raise CommandError("Cannot write chat migration snapshot: %s" % error)
            self.stdout.write("  Snapshot written: %s" % self.snapshot_path)

        slowest_statement_seconds, slowest_statement_kind = max(
            statement_timings,
            default=(0.0, "none"),
        )
        self.stdout.write(
            "  Slowest SQL statement: %.3fs (%s)"
            % (slowest_statement_seconds, slowest_statement_kind)
        )
        if slowest_statement_seconds >= 8:
            raise CommandError(
                "A preflight SQL statement exceeded the 8-second server limit."
            )

    def _run_preflight(self):
        started = time.monotonic()
        if self.require_online_ddl:
            self.stdout.write(
                "  Deployment requirement: stop all chat writes before applying "
                "migrations 0027-0029, then run chat_room_verify before reopening chat."
            )
        self._check_online_ddl_support()
        maximum_batch_seconds = 0.0
        pair_to_room = {}
        duplicate_pairs = []
        empty_rooms = []
        unsafe_rooms = []
        room_count = 0
        last_room_id = 0

        with connection.cursor() as cursor:
            room_columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, Room._meta.db_table
                )
            }
        generalized_schema = "room_type" in room_columns

        while True:
            batch_started = time.monotonic()
            rooms = Room.objects.filter(id__gt=last_room_id)
            if generalized_schema:
                # After the migration, groups and channels legitimately have
                # any number of members. Re-running this read-only command must
                # validate only the legacy-compatible direct-room population.
                rooms = rooms.filter(room_type=Room.Type.DIRECT)
            if "singleton_key" in room_columns:
                rooms = rooms.exclude(singleton_key="lobby")
            if "archived_at" in room_columns:
                rooms = rooms.filter(archived_at__isnull=True)
            room_ids = list(
                rooms.order_by("id").values_list("id", flat=True)[:PREFLIGHT_BATCH_SIZE]
            )
            if not room_ids:
                break
            last_room_id = room_ids[-1]
            members = {room_id: [] for room_id in room_ids}
            for room_id, user_id in (
                UserRoom.objects.filter(room_id__in=room_ids)
                .order_by("room_id", "user_id")
                .values_list("room_id", "user_id")
            ):
                members[room_id].append(user_id)
            batch_empty_room_ids = [
                room_id for room_id in room_ids if not set(members[room_id])
            ]
            empty_rooms_with_messages = set(
                Message.objects.filter(room_id__in=batch_empty_room_ids)
                .values_list("room_id", flat=True)
                .distinct()
            )
            for room_id in room_ids:
                user_ids = sorted(set(members[room_id]))
                if not user_ids:
                    if room_id in empty_rooms_with_messages:
                        unsafe_rooms.append((room_id, 0, "contains messages"))
                    else:
                        empty_rooms.append(room_id)
                    continue
                if len(user_ids) > 2:
                    unsafe_rooms.append((room_id, len(user_ids), "too many members"))
                    continue
                pair = (user_ids[0], user_ids[-1])
                prior_room = pair_to_room.get(pair)
                if prior_room is not None:
                    duplicate_pairs.append((prior_room, room_id, pair))
                else:
                    pair_to_room[pair] = room_id
            room_count += len(room_ids)
            maximum_batch_seconds = max(
                maximum_batch_seconds, time.monotonic() - batch_started
            )

        null_messages = Message.objects.filter(room__isnull=True).count()
        null_memberships = UserRoom.objects.filter(room__isnull=True).count()
        orphan_memberships = UserRoom.objects.exclude(
            user_id__in=Profile.objects.values("id")
        ).count()
        elapsed = time.monotonic() - started

        self.stdout.write("Generalized chat-room preflight")
        self.stdout.write(
            "  %s: %s"
            % ("Direct rooms" if generalized_schema else "Legacy rooms", room_count)
        )
        self.stdout.write("  Lobby messages: %s" % null_messages)
        self.stdout.write("  Lobby trackers: %s" % null_memberships)
        self.stdout.write("  Orphan memberships: %s" % orphan_memberships)
        self.stdout.write("  Duplicate DM rooms to merge: %s" % len(duplicate_pairs))
        self.stdout.write("  Empty rooms to archive: %s" % len(empty_rooms))
        self.stdout.write("  Unsafe malformed rooms: %s" % len(unsafe_rooms))
        self.stdout.write("  Slowest 500-room batch: %.3fs" % maximum_batch_seconds)
        self.stdout.write("  Total elapsed: %.3fs" % elapsed)

        if duplicate_pairs:
            self.stdout.write("  Duplicate examples: %r" % duplicate_pairs[:10])
        if empty_rooms:
            self.stdout.write("  Empty examples: %r" % empty_rooms[:10])
        if unsafe_rooms:
            self.stdout.write("  Unsafe examples: %r" % unsafe_rooms[:10])
        if maximum_batch_seconds >= 8:
            raise CommandError(
                "A representative 500-room backfill batch exceeded eight seconds."
            )
        if unsafe_rooms or (generalized_schema and duplicate_pairs):
            raise CommandError("Legacy chat data cannot be migrated safely.")
        self.stdout.write(self.style.SUCCESS("Preflight passed."))
        if not self.snapshot_path:
            return None
        lobby_message_count = null_messages
        if generalized_schema:
            lobby_message_count = Message.objects.filter(
                room__singleton_key="lobby"
            ).count()
        return {
            "version": 1,
            "profiles": Profile.objects.count(),
            "messages": Message.objects.count(),
            "message_reactions": MessageReaction.objects.count(),
            "replies": Message.objects.filter(reply_to__isnull=False).count(),
            "lobby_messages": lobby_message_count,
        }

    def _check_online_ddl_support(self):
        if connection.vendor != "mysql" or not getattr(
            connection, "mysql_is_mariadb", False
        ):
            return
        version = connection.mysql_version
        if version < (11, 2):
            message = (
                "Generalized chat production migrations require MariaDB 11.2 "
                "or newer for online COPY fallback."
            )
            if self.require_online_ddl:
                raise CommandError(message)
            self.stderr.write(self.style.WARNING(message))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                [Message._meta.db_table],
            )
            row = cursor.fetchone()
        if not row or row[0].upper() != "INNODB":
            message = "The chat message table must use InnoDB for online DDL."
            if self.require_online_ddl:
                raise CommandError(message)
            self.stderr.write(self.style.WARNING(message))
            return
        self.stdout.write(
            "  Online DDL: MariaDB %s, InnoDB" % ".".join(str(part) for part in version)
        )
