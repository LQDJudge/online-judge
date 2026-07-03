from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from chat_box.models import (
    ChatModerationLog,
    Room,
    Message,
    UserRoom,
    get_user_room_list,
)
from chat_box.utils import encrypt_url, get_unread_boxes
from chat_box.views import ChatView, get_status_context
from judge.models import Notification, Profile
from judge.models.notification import NotificationCategory


class DeleteMessageCacheTest(TestCase):
    """Test that deleting a message properly invalidates room cache."""

    def setUp(self):
        cache.clear()

        # Create users
        self.user1 = User.objects.create_user(
            username="chatuser1", password="password123"
        )
        self.profile1, _ = Profile.objects.get_or_create(user=self.user1)

        self.user2 = User.objects.create_user(
            username="chatuser2", password="password123"
        )
        self.profile2, _ = Profile.objects.get_or_create(user=self.user2)

        # Create a room
        self.room = Room.objects.create(last_msg_id=None)
        UserRoom.objects.create(room=self.room, user=self.profile1)
        UserRoom.objects.create(room=self.room, user=self.profile2)

        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_delete_last_message_updates_room_cache(self):
        """When the last message is deleted, room.last_msg_id should update."""
        # Create two messages
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="First message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        msg2 = Message.objects.create(
            room=self.room, author=self.profile2, body="Second message"
        )
        self.room.last_msg_id = msg2.id
        self.room.save()

        # Verify initial state
        self.assertEqual(self.room.last_msg_id, msg2.id)

        # Prime the cache
        room_instance = Room(id=self.room.id)
        cached_last_msg = room_instance.get_last_message()
        self.assertEqual(cached_last_msg, "Second message")

        # Delete the last message via the view
        self.client.login(username="chatuser2", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg2.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Verify room.last_msg_id is updated
        self.room.refresh_from_db()
        self.assertEqual(self.room.last_msg_id, msg1.id)

        # Verify cache is invalidated - should now return first message
        room_instance = Room(id=self.room.id)
        room_instance._cached_dict = None  # Clear instance cache
        cached_last_msg = room_instance.get_last_message()
        self.assertEqual(cached_last_msg, "First message")

    def test_delete_non_last_message_keeps_last_msg_id(self):
        """When a non-last message is deleted, room.last_msg_id stays the same."""
        # Create two messages
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="First message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        msg2 = Message.objects.create(
            room=self.room, author=self.profile2, body="Second message"
        )
        self.room.last_msg_id = msg2.id
        self.room.save()

        # Delete the first (non-last) message
        self.client.login(username="chatuser1", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Verify room.last_msg_id is unchanged
        self.room.refresh_from_db()
        self.assertEqual(self.room.last_msg_id, msg2.id)

    def test_delete_only_message_sets_last_msg_id_to_none(self):
        """When the only message is deleted, room.last_msg_id becomes None."""
        # Create one message
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="Only message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        # Delete the only message
        self.client.login(username="chatuser1", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Verify room.last_msg_id is None
        self.room.refresh_from_db()
        self.assertIsNone(self.room.last_msg_id)

    def test_delete_message_requires_author_or_staff(self):
        """Users can only delete their own messages (unless staff)."""
        # Create a message by user1
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="User1's message"
        )

        # Try to delete as user2 (should fail)
        self.client.login(username="chatuser2", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)

        # Message should still exist and not be hidden
        msg1.refresh_from_db()
        self.assertFalse(msg1.hidden)

    def test_delete_message_decrements_unread_count(self):
        """When a message is deleted, unread_count should decrement for users who haven't seen it."""
        # Set last_seen to past for user2
        past_time = timezone.now() - timezone.timedelta(hours=1)
        user_room2 = UserRoom.objects.get(room=self.room, user=self.profile2)
        user_room2.last_seen = past_time
        user_room2.unread_count = 2
        user_room2.save()

        # Create a message from user1 (after user2's last_seen)
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="New message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        # Verify initial unread count
        user_room2.refresh_from_db()
        self.assertEqual(user_room2.unread_count, 2)

        # Delete the message as user1
        self.client.login(username="chatuser1", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Verify unread_count is decremented
        user_room2.refresh_from_db()
        self.assertEqual(user_room2.unread_count, 1)

    def test_delete_message_does_not_decrement_for_seen_users(self):
        """When a message is deleted, unread_count should not change for users who have seen it."""
        # Set last_seen to future for user2 (they've seen the message)
        future_time = timezone.now() + timezone.timedelta(hours=1)
        user_room2 = UserRoom.objects.get(room=self.room, user=self.profile2)
        user_room2.last_seen = future_time
        user_room2.unread_count = 0
        user_room2.save()

        # Create a message from user1
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="New message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        # Delete the message as user1
        self.client.login(username="chatuser1", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Verify unread_count is unchanged
        user_room2.refresh_from_db()
        self.assertEqual(user_room2.unread_count, 0)


class ChatMuteTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.mod_user = User.objects.create_user(
            username="chatmod", password="password123"
        )
        self.mod_profile, _ = Profile.objects.get_or_create(user=self.mod_user)
        self.author_user = User.objects.create_user(
            username="muteduser", password="password123"
        )
        self.author_profile, _ = Profile.objects.get_or_create(user=self.author_user)
        self.admin_user = User.objects.create_superuser(
            username="chatadmin", password="password123"
        )
        self.admin_profile, _ = Profile.objects.get_or_create(user=self.admin_user)
        self.temp_perm = Permission.objects.get(codename="change_comment")
        self.mod_user.user_permissions.add(self.temp_perm)

    def tearDown(self):
        cache.clear()

    def test_temporary_mute_requires_reason_for_moderator(self):
        message = Message.objects.create(
            room=None, author=self.author_profile, body="bad lobby message"
        )
        self.client.login(username="chatmod", password="password123")
        response = self.client.post(
            "/chat/mute/",
            {"message": message.id, "mute_type": "temporary", "reason": ""},
        )
        self.assertEqual(response.status_code, 400)
        self.author_profile.refresh_from_db()
        self.assertFalse(self.author_profile.mute)

    def test_temporary_mute_escalates_and_notifies_user(self):
        old_message = Message.objects.create(
            room=None, author=self.author_profile, body="old bad lobby message"
        )
        ChatModerationLog.log_action(
            message=old_message,
            action="mute_temp",
            reason="Previous warning",
            mute_duration_days=1,
        )
        message = Message.objects.create(
            room=None, author=self.author_profile, body="new bad lobby message"
        )

        before = timezone.now()
        self.client.login(username="chatmod", password="password123")
        response = self.client.post(
            "/chat/mute/",
            {
                "message": message.id,
                "mute_type": "temporary",
                "reason": "Repeated spam",
            },
        )
        self.assertEqual(response.status_code, 200)

        self.author_profile.refresh_from_db()
        self.assertTrue(self.author_profile.mute)
        self.assertEqual(self.author_profile.mute_reason, "Repeated spam")
        self.assertIsNotNone(self.author_profile.mute_until)
        self.assertGreaterEqual(
            self.author_profile.mute_until, before + timezone.timedelta(days=2)
        )
        self.assertLessEqual(
            self.author_profile.mute_until, timezone.now() + timezone.timedelta(days=3)
        )

        log = ChatModerationLog.objects.get(message=message)
        self.assertEqual(log.action, "mute_temp")
        self.assertEqual(log.reason, "Repeated spam")
        self.assertEqual(log.mute_duration_days, 2)

        notification = Notification.objects.get(owner=self.author_profile)
        self.assertEqual(notification.category, NotificationCategory.CHAT_MUTE)
        self.assertIn("Repeated spam", notification.html_link)

    def test_moderator_cannot_permanently_mute(self):
        message = Message.objects.create(
            room=None, author=self.author_profile, body="severe lobby message"
        )
        self.client.login(username="chatmod", password="password123")
        response = self.client.post(
            "/chat/mute/",
            {
                "message": message.id,
                "mute_type": "permanent",
                "reason": "Too severe",
            },
        )
        self.assertEqual(response.status_code, 400)

        self.author_profile.refresh_from_db()
        self.assertFalse(self.author_profile.mute)

    def test_permanent_mute_allowed_for_superuser_without_reason(self):
        message = Message.objects.create(
            room=None, author=self.author_profile, body="severe lobby message"
        )
        self.client.login(username="chatadmin", password="password123")
        response = self.client.post(
            "/chat/mute/",
            {"message": message.id, "mute_type": "permanent", "reason": ""},
        )
        self.assertEqual(response.status_code, 200)

        self.author_profile.refresh_from_db()
        self.assertTrue(self.author_profile.mute)
        self.assertIsNone(self.author_profile.mute_until)

        log = ChatModerationLog.objects.get(message=message)
        self.assertEqual(log.action, "mute_perm")
        self.assertIsNone(log.mute_until)


class ChatPaginationTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="chatpager")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

    def tearDown(self):
        cache.clear()

    def test_get_message_page_uses_id_page_then_row_hydration(self):
        messages = [
            Message.objects.create(
                room=None,
                author=self.profile,
                body="message %(index)s" % {"index": index},
            )
            for index in range(6)
        ]
        messages[2].hidden = True
        messages[2].save(update_fields=["hidden"])

        view = ChatView()
        view.room_id = None
        last_id = messages[-1].id + 1

        with CaptureQueriesContext(connection) as queries:
            page = view.get_message_page(last_id=last_id, page_size=3)

        self.assertEqual(len(queries), 2)
        self.assertEqual(
            [message.id for message in page],
            [
                messages[5].id,
                messages[4].id,
                messages[3].id,
            ],
        )

    def test_get_message_page_stops_after_empty_id_page(self):
        view = ChatView()
        view.room_id = None

        with CaptureQueriesContext(connection) as queries:
            page = view.get_message_page(last_id=1, page_size=3)

        self.assertEqual(len(queries), 1)
        self.assertEqual(page, [])


class ChatSelfRoomTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            username="selfchat", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.other_user = User.objects.create_user(username="selfchatother")
        self.other_profile, _ = Profile.objects.get_or_create(user=self.other_user)

    def tearDown(self):
        cache.clear()

    def test_self_room_is_single_member_and_not_existing_dm(self):
        other_room = Room.get_or_create_room(self.profile, self.other_profile)

        self_room = Room.get_or_create_room(self.profile, self.profile)
        self.assertNotEqual(self_room.id, other_room.id)
        self.assertEqual(UserRoom.objects.filter(room=self_room).count(), 1)
        self.assertTrue(
            UserRoom.objects.filter(room=self_room, user=self.profile).exists()
        )
        self.assertEqual(self_room.other_user_id(self.profile), self.profile.id)

        same_self_room = Room.get_or_create_room(self.profile, self.profile)
        self.assertEqual(same_self_room.id, self_room.id)
        self.assertEqual(UserRoom.objects.filter(room=self_room).count(), 1)

    def test_get_or_create_room_accepts_self_chat(self):
        self.client.login(username="selfchat", password="password123")

        response = self.client.get(
            "/chat/get_or_create_room",
            {"other": encrypt_url(self.profile.id, self.profile.id)},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["other_user_id"], self.profile.id)
        self.assertEqual(
            UserRoom.objects.filter(room_id=payload["room"], user=self.profile).count(),
            1,
        )
        self.assertEqual(UserRoom.objects.filter(room_id=payload["room"]).count(), 1)

    def test_self_room_appears_in_recent_status_context(self):
        self_room = Room.get_or_create_room(self.profile, self.profile)
        Message.objects.create(room=self_room, author=self.profile, body="private note")
        self_room.last_msg_id = Message.objects.filter(room=self_room).first().id
        self_room.save(update_fields=["last_msg_id"])
        Room.dirty_cache(self_room.id)
        get_user_room_list.dirty(self.profile.id)

        recent = get_status_context(self.profile)[0]["user_list"]

        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["user"].id, self.profile.id)
        self.assertTrue(recent[0]["is_self"])
        self.assertEqual(recent[0]["room"], self_room.id)
        self.assertEqual(recent[0]["last_msg"], "private note")

    def test_can_post_message_to_self_room(self):
        self_room = Room.get_or_create_room(self.profile, self.profile)
        self.client.login(username="selfchat", password="password123")

        response = self.client.post(
            "/chat/post/",
            {"room": self_room.id, "body": "remember this", "tmp_id": "self-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Message.objects.filter(
                room=self_room, author=self.profile, body="remember this"
            ).exists()
        )
        self.assertEqual(UserRoom.objects.get(room=self_room).unread_count, 0)


class UnreadBoxesCacheTest(TestCase):
    """Test that get_unread_boxes cache is properly invalidated."""

    def setUp(self):
        cache.clear()

        # Create users
        self.user1 = User.objects.create_user(
            username="unreaduser1", password="password123"
        )
        self.profile1, _ = Profile.objects.get_or_create(user=self.user1)

        self.user2 = User.objects.create_user(
            username="unreaduser2", password="password123"
        )
        self.profile2, _ = Profile.objects.get_or_create(user=self.user2)

        # Create a room
        self.room = Room.objects.create(last_msg_id=None)
        UserRoom.objects.create(room=self.room, user=self.profile1, unread_count=0)
        UserRoom.objects.create(room=self.room, user=self.profile2, unread_count=1)

        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_get_unread_boxes_returns_correct_count(self):
        """get_unread_boxes should return count of rooms with unread messages."""
        count = get_unread_boxes(self.profile2)
        self.assertEqual(count, 1)

        count = get_unread_boxes(self.profile1)
        self.assertEqual(count, 0)

    def test_toggle_ignore_invalidates_unread_boxes_cache(self):
        """Toggling ignore should invalidate get_unread_boxes cache."""
        # Prime the cache
        initial_count = get_unread_boxes(self.profile2)
        self.assertEqual(initial_count, 1)

        # Ignore user1 (who is in the room with unread messages)
        self.client.login(username="unreaduser2", password="password123")
        response = self.client.get(f"/chat/toggle_ignore/{self.profile1.id}?next=/")
        self.assertEqual(response.status_code, 302)

        # After ignoring, the room should be excluded from unread count
        new_count = get_unread_boxes(self.profile2)
        self.assertEqual(new_count, 0)

    def test_delete_message_invalidates_unread_boxes_cache(self):
        """Deleting a message should invalidate get_unread_boxes cache."""
        # Set up user2 with unread message
        past_time = timezone.now() - timezone.timedelta(hours=1)
        user_room2 = UserRoom.objects.get(room=self.room, user=self.profile2)
        user_room2.last_seen = past_time
        user_room2.unread_count = 1
        user_room2.save()

        # Create a message
        msg1 = Message.objects.create(
            room=self.room, author=self.profile1, body="Test message"
        )
        self.room.last_msg_id = msg1.id
        self.room.save()

        # Prime the cache
        initial_count = get_unread_boxes(self.profile2)
        self.assertEqual(initial_count, 1)

        # Delete the message
        self.client.login(username="unreaduser1", password="password123")
        response = self.client.post(
            "/chat/delete/",
            {"message": msg1.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)

        # Cache should be invalidated, unread count should be 0
        new_count = get_unread_boxes(self.profile2)
        self.assertEqual(new_count, 0)


class CleanupOldRoomsTest(TestCase):
    """Test that old rooms are cleaned up when user exceeds limit."""

    def setUp(self):
        cache.clear()

        # Create main user
        self.user1 = User.objects.create_user(
            username="mainuser", password="password123"
        )
        self.profile1, _ = Profile.objects.get_or_create(user=self.user1)

    def tearDown(self):
        cache.clear()

    def _create_other_user(self, index):
        """Helper to create another user."""
        user = User.objects.create_user(
            username=f"otheruser{index}", password="password123"
        )
        profile, _ = Profile.objects.get_or_create(user=user)
        return profile

    def _create_room_with_message(self, user1, user2, msg_id_offset=0):
        """Helper to create a room with a message."""
        room = Room.objects.create(last_msg_id=None)
        UserRoom.objects.create(room=room, user=user1, last_seen=timezone.now())
        UserRoom.objects.create(room=room, user=user2, last_seen=timezone.now())

        # Create a message to set last_msg_id
        msg = Message.objects.create(
            room=room, author=user1, body=f"Message {msg_id_offset}"
        )
        room.last_msg_id = msg.id
        room.save()

        Room.dirty_cache(room.id)
        get_user_room_list.dirty(user1.id)
        get_user_room_list.dirty(user2.id)

        return room

    @patch.object(Room, "MAX_ROOMS_PER_USER", 5)
    def test_cleanup_deletes_oldest_rooms(self):
        """When user exceeds limit, oldest rooms (by last_msg_id) are deleted."""
        # Create 5 rooms (at the limit)
        other_users = [self._create_other_user(i) for i in range(6)]
        rooms = []
        for i, other in enumerate(other_users[:5]):
            room = self._create_room_with_message(self.profile1, other, i)
            rooms.append(room)

        # Verify we have 5 rooms
        self.assertEqual(
            UserRoom.objects.filter(user=self.profile1)
            .exclude(room__isnull=True)
            .count(),
            5,
        )

        # Create one more room (exceeds limit)
        new_room = Room.get_or_create_room(self.profile1, other_users[5])

        # Should now have 5 rooms (oldest deleted)
        self.assertEqual(
            UserRoom.objects.filter(user=self.profile1)
            .exclude(room__isnull=True)
            .count(),
            5,
        )

        # The oldest room (rooms[0]) should be deleted
        self.assertFalse(Room.objects.filter(id=rooms[0].id).exists())

        # The new room should exist
        self.assertTrue(Room.objects.filter(id=new_room.id).exists())

    @patch.object(Room, "MAX_ROOMS_PER_USER", 5)
    def test_cleanup_does_not_run_under_limit(self):
        """When user is under limit, no rooms are deleted."""
        # Create 3 rooms (under limit)
        other_users = [self._create_other_user(i) for i in range(3)]
        rooms = []
        for i, other in enumerate(other_users):
            room = self._create_room_with_message(self.profile1, other, i)
            rooms.append(room)

        # Run cleanup
        Room.cleanup_old_rooms(self.profile1)

        # All rooms should still exist
        self.assertEqual(
            UserRoom.objects.filter(user=self.profile1)
            .exclude(room__isnull=True)
            .count(),
            3,
        )
        for room in rooms:
            self.assertTrue(Room.objects.filter(id=room.id).exists())

    @patch.object(Room, "MAX_ROOMS_PER_USER", 3)
    def test_cleanup_invalidates_other_user_caches(self):
        """When a room is deleted, caches for both users are invalidated."""
        # Create 4 rooms (over limit of 3)
        other_users = [self._create_other_user(i) for i in range(4)]
        for i, other in enumerate(other_users):
            self._create_room_with_message(self.profile1, other, i)

        # Prime the cache for the first other user
        other_rooms_before = get_user_room_list(other_users[0].id)
        self.assertEqual(len(other_rooms_before), 1)

        # Run cleanup (should delete oldest room)
        Room.cleanup_old_rooms(self.profile1)

        # Cache should be invalidated - other user should have 0 rooms
        other_rooms_after = get_user_room_list(other_users[0].id)
        self.assertEqual(len(other_rooms_after), 0)

    @patch.object(Room, "MAX_ROOMS_PER_USER", 3)
    def test_cleanup_deletes_messages_with_room(self):
        """When a room is deleted, its messages are also deleted (cascade)."""
        # Create 4 rooms
        other_users = [self._create_other_user(i) for i in range(4)]
        rooms = []
        for i, other in enumerate(other_users):
            room = self._create_room_with_message(self.profile1, other, i)
            rooms.append(room)

        # Get message count for first room
        first_room_msg_count = Message.objects.filter(room=rooms[0]).count()
        self.assertEqual(first_room_msg_count, 1)

        # Run cleanup
        Room.cleanup_old_rooms(self.profile1)

        # Messages from deleted room should be gone
        self.assertEqual(Message.objects.filter(room=rooms[0]).count(), 0)

    @patch.object(Room, "MAX_ROOMS_PER_USER", 5)
    def test_get_or_create_existing_room_does_not_trigger_cleanup(self):
        """Getting an existing room should not trigger cleanup."""
        # Create 5 rooms (at limit)
        other_users = [self._create_other_user(i) for i in range(5)]
        rooms = []
        for i, other in enumerate(other_users):
            room = self._create_room_with_message(self.profile1, other, i)
            rooms.append(room)

        # Get existing room (should not trigger cleanup)
        existing_room = Room.get_or_create_room(self.profile1, other_users[2])

        # All 5 rooms should still exist
        self.assertEqual(
            UserRoom.objects.filter(user=self.profile1)
            .exclude(room__isnull=True)
            .count(),
            5,
        )
        self.assertEqual(existing_room.id, rooms[2].id)
