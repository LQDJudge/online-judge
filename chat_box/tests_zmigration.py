import time
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from chat_box.models import Message, MessageReaction, Room, RoomRedirect, UserRoom


class GeneralizedRoomMigrationTests(TransactionTestCase):
    fixtures = ["language_small"]
    migrate_from = ("chat_box", "0026_message_reply_to")
    migrate_to = ("chat_box", "0029_finalize_generalized_rooms")

    def _post_teardown(self):
        # A setup/assertion failure can leave the schema at migrate_from. Restore
        # it before Django flushes using the current model definitions.
        MigrationExecutor(connection).migrate([self.migrate_to])
        super()._post_teardown()
        # TransactionTestCase flushes data after the schema test. Restore the
        # singleton expected by later tests and by Profile creation signals.
        Room.objects.get_or_create(
            singleton_key="lobby",
            defaults={
                "room_type": Room.Type.CHANNEL,
                "channel_kind": Room.ChannelKind.LOBBY,
                "name": "Lobby",
            },
        )

    def test_legacy_lobby_and_dm_backfill_is_lossless_and_bounded(self):
        Message.objects.all().delete()
        UserRoom.objects.all().delete()
        Room.objects.all().delete()

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model("auth", "User")
        Language = old_apps.get_model("judge", "Language")
        Profile = old_apps.get_model("judge", "Profile")
        OldMessage = old_apps.get_model("chat_box", "Message")
        OldMessageReaction = old_apps.get_model("chat_box", "MessageReaction")
        OldRoom = old_apps.get_model("chat_box", "Room")
        OldUserRoom = old_apps.get_model("chat_box", "UserRoom")

        language = Language.objects.first()
        profiles = []
        for index in range(3):
            user = User.objects.create(
                username="migration_user_%s" % index,
                password="!",
            )
            profiles.append(Profile.objects.create(user=user, language=language))

        legacy_dm = OldRoom.objects.create()
        first_dm_membership = OldUserRoom.objects.create(
            room=legacy_dm, user=profiles[0]
        )
        second_dm_membership = OldUserRoom.objects.create(
            room=legacy_dm, user=profiles[1]
        )
        dm_message = OldMessage.objects.create(
            room=legacy_dm,
            author=profiles[0],
            body="Legacy direct message",
        )
        first_unread = OldMessage.objects.create(
            room=legacy_dm,
            author=profiles[1],
            body="First unread direct message",
        )
        own_message_after_unread = OldMessage.objects.create(
            room=legacy_dm,
            author=profiles[0],
            body="Own message after unread message",
        )
        second_unread = OldMessage.objects.create(
            room=legacy_dm,
            author=profiles[1],
            body="Second unread direct message",
        )
        OldUserRoom.objects.filter(id=first_dm_membership.id).update(unread_count=2)
        OldUserRoom.objects.filter(id=second_dm_membership.id).update(unread_count=1)
        legacy_dm.last_msg_id = second_unread.id
        legacy_dm.save(update_fields=["last_msg_id"])

        duplicate_dm = OldRoom.objects.create()
        duplicate_first_membership = OldUserRoom.objects.create(
            room=duplicate_dm, user=profiles[0]
        )
        duplicate_second_membership = OldUserRoom.objects.create(
            room=duplicate_dm, user=profiles[1]
        )
        duplicate_parent = OldMessage.objects.create(
            room=duplicate_dm,
            author=profiles[0],
            body="Message in duplicate direct room",
        )
        duplicate_reply = OldMessage.objects.create(
            room=duplicate_dm,
            author=profiles[1],
            body="Reply in duplicate direct room",
            reply_to=duplicate_parent,
        )
        duplicate_hidden = OldMessage.objects.create(
            room=duplicate_dm,
            author=profiles[1],
            body="Hidden duplicate-room message",
            hidden=True,
        )
        duplicate_reaction = OldMessageReaction.objects.create(
            message=duplicate_reply,
            user=profiles[0],
            reaction="love",
        )
        OldUserRoom.objects.filter(id=duplicate_first_membership.id).update(
            unread_count=1,
            last_seen=timezone.now(),
        )
        OldUserRoom.objects.filter(id=duplicate_second_membership.id).update(
            unread_count=1,
            last_seen=timezone.now(),
        )
        duplicate_dm.last_msg_id = duplicate_reply.id
        duplicate_dm.save(update_fields=["last_msg_id"])

        legacy_self_dm = OldRoom.objects.create()
        OldUserRoom.objects.create(room=legacy_self_dm, user=profiles[2])
        self_message = OldMessage.objects.create(
            room=legacy_self_dm,
            author=profiles[2],
            body="Legacy Saved Messages",
        )
        # Exercise a stale legacy pointer into another room. The migration must
        # derive both canonical-room selection and previews from actual history.
        legacy_self_dm.last_msg_id = first_unread.id
        legacy_self_dm.save(update_fields=["last_msg_id"])

        duplicate_self_dm = OldRoom.objects.create()
        OldUserRoom.objects.create(room=duplicate_self_dm, user=profiles[2])
        duplicate_self_message = OldMessage.objects.create(
            room=duplicate_self_dm,
            author=profiles[2],
            body="Newer legacy Saved Messages",
        )
        duplicate_self_dm.last_msg_id = duplicate_self_message.id
        duplicate_self_dm.save(update_fields=["last_msg_id"])

        empty_legacy_room = OldRoom.objects.create(last_msg_id=2**31 - 1)

        unsafe_empty_room = OldRoom.objects.create()
        unsafe_empty_message = OldMessage.objects.create(
            room=unsafe_empty_room,
            author=profiles[0],
            body="An empty room cannot retain messages",
        )
        unsafe_preflight_output = StringIO()
        with self.assertRaises(CommandError):
            call_command("chat_room_preflight", stdout=unsafe_preflight_output)
        self.assertIn(
            "Unsafe malformed rooms: 1",
            unsafe_preflight_output.getvalue(),
        )
        OldUserRoom.objects.create(room=unsafe_empty_room, user=profiles[0])
        unsafe_empty_room.last_msg_id = unsafe_empty_message.id
        unsafe_empty_room.save(update_fields=["last_msg_id"])

        older_tracker = OldUserRoom.objects.create(
            room=None,
            user=profiles[0],
            unread_count=2,
        )
        newer_tracker = OldUserRoom.objects.create(
            room=None,
            user=profiles[0],
            unread_count=1,
        )
        old_seen = timezone.now() - timezone.timedelta(days=1)
        OldUserRoom.objects.filter(id=older_tracker.id).update(last_seen=old_seen)
        OldUserRoom.objects.filter(id=newer_tracker.id).update(last_seen=timezone.now())
        lobby_message = OldMessage.objects.create(
            room=None,
            author=profiles[2],
            body="Legacy Lobby message",
        )

        preflight_output = StringIO()
        call_command("chat_room_preflight", stdout=preflight_output)
        self.assertIn("Preflight passed.", preflight_output.getvalue())
        self.assertIn(
            "Duplicate DM rooms to merge: 2",
            preflight_output.getvalue(),
        )
        self.assertIn("Empty rooms to archive: 1", preflight_output.getvalue())

        statement_timings = []

        def time_statement(execute, sql, params, many, context):
            started = time.monotonic()
            try:
                return execute(sql, params, many, context)
            finally:
                statement_timings.append(time.monotonic() - started)

        try:
            with connection.execute_wrapper(time_statement):
                executor = MigrationExecutor(connection)
                executor.migrate([self.migrate_to])

            lobby = Room.objects.get(singleton_key="lobby")
            self.assertEqual(
                Message.objects.get(id=lobby_message.id).room_id,
                lobby.id,
            )
            self.assertEqual(Message.objects.filter(room=lobby).count(), 1)
            self.assertEqual(
                UserRoom.objects.filter(room=lobby, user_id=profiles[0].id).count(),
                1,
            )
            merged_tracker = UserRoom.objects.get(
                room=lobby,
                user_id=profiles[0].id,
            )
            self.assertEqual(merged_tracker.unread_count, 2)
            self.assertEqual(
                UserRoom.objects.filter(room=lobby).count(),
                len(profiles),
            )

            self.assertFalse(Room.objects.filter(id=legacy_dm.id).exists())
            dm = Room.objects.get(id=duplicate_dm.id)
            self.assertEqual(
                RoomRedirect.objects.get(old_room_id=legacy_dm.id).canonical_room_id,
                dm.id,
            )
            self.assertEqual(dm.room_type, Room.Type.DIRECT)
            self.assertEqual(dm.direct_user_low_id, profiles[0].id)
            self.assertEqual(dm.direct_user_high_id, profiles[1].id)
            self.assertEqual(dm.last_msg_id, duplicate_reply.id)
            self.assertEqual(
                Message.objects.get(id=dm_message.id).body,
                "Legacy direct message",
            )
            self.assertEqual(
                Message.objects.filter(
                    id__in=[
                        dm_message.id,
                        first_unread.id,
                        own_message_after_unread.id,
                        second_unread.id,
                        duplicate_parent.id,
                        duplicate_reply.id,
                        duplicate_hidden.id,
                    ],
                    room=dm,
                ).count(),
                7,
            )
            self.assertTrue(Message.objects.get(id=duplicate_hidden.id).hidden)
            self.assertEqual(
                Message.objects.get(id=duplicate_reply.id).reply_to_id,
                duplicate_parent.id,
            )
            self.assertTrue(
                MessageReaction.objects.filter(
                    id=duplicate_reaction.id,
                    message_id=duplicate_reply.id,
                ).exists()
            )
            first_membership = UserRoom.objects.get(
                room=dm,
                user_id=profiles[0].id,
            )
            second_membership = UserRoom.objects.get(
                room=dm,
                user_id=profiles[1].id,
            )
            self.assertEqual(
                Message.objects.filter(
                    room=dm,
                    id__gt=first_membership.last_read_message_id or 0,
                    hidden=False,
                    kind=Message.Kind.USER,
                )
                .exclude(author_id=profiles[0].id)
                .count(),
                3,
            )
            self.assertEqual(
                Message.objects.filter(
                    room=dm,
                    id__gt=second_membership.last_read_message_id or 0,
                    hidden=False,
                    kind=Message.Kind.USER,
                )
                .exclude(author_id=profiles[1].id)
                .count(),
                2,
            )
            self.assertEqual(first_membership.unread_count, 3)
            self.assertEqual(second_membership.unread_count, 2)
            self.assertLess(
                first_membership.last_read_message_id,
                first_unread.id,
            )
            self.assertLess(
                second_membership.last_read_message_id,
                own_message_after_unread.id,
            )
            ambiguous_history = Room.objects.get(id=legacy_self_dm.id)
            self.assertEqual(ambiguous_history.archive_reason, "ambiguous_legacy")
            self.assertIsNotNone(ambiguous_history.archived_at)
            self.assertIsNone(ambiguous_history.direct_user_low_id)
            self.assertIsNone(ambiguous_history.direct_user_high_id)
            self.assertEqual(ambiguous_history.last_msg_id, self_message.id)
            self.assertEqual(
                Message.objects.get(id=self_message.id).room_id,
                ambiguous_history.id,
            )
            self.assertFalse(
                RoomRedirect.objects.filter(old_room_id=legacy_self_dm.id).exists()
            )
            saved_messages = Room.objects.get(id=duplicate_self_dm.id)
            self.assertEqual(saved_messages.direct_user_low_id, profiles[2].id)
            self.assertEqual(saved_messages.direct_user_high_id, profiles[2].id)
            self.assertEqual(
                Message.objects.get(id=duplicate_self_message.id).room_id,
                saved_messages.id,
            )
            self.assertEqual(saved_messages.last_msg_id, duplicate_self_message.id)
            empty_room = Room.objects.get(id=empty_legacy_room.id)
            self.assertEqual(empty_room.archive_reason, "empty_legacy")
            self.assertIsNotNone(empty_room.archived_at)
            self.assertIsNone(empty_room.direct_user_low_id)
            self.assertIsNone(empty_room.direct_user_high_id)
            self.assertIsNone(empty_room.last_msg_id)
            self.assertLess(max(statement_timings, default=0), 8)
        finally:
            MigrationExecutor(connection).migrate([self.migrate_to])
