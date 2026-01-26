from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.cache import cache

from chat_box.models import Room, Message, UserRoom
from judge.models import Profile


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
