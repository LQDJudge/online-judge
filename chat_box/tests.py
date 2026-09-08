from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from chat_box.models import (
    ChatModerationLog,
    Room,
    Message,
    RoomMute,
    UserRoom,
)
from chat_box.utils import encrypt_url, get_unread_boxes
from chat_box.views import ChatView, get_status_context, get_unread_count
from judge.models import (
    Language,
    Notification,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)
from judge.models.notification import NotificationCategory


def lobby_room():
    return Room.objects.get(singleton_key="lobby")


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
        self.room = Room.get_or_create_room(self.profile1, self.profile2)

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
        self.assertEqual(response.status_code, 403)

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

        # Cursor-derived unread state is authoritative during compatibility.
        user_room2.refresh_from_db()
        self.assertEqual(user_room2.unread_count, 2)
        self.assertEqual(get_unread_count(self.room, self.profile2), 0)

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
    fixtures = ["language_small"]

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
        self.lobby = lobby_room()
        UserRoom.objects.filter(room=self.lobby, user=self.mod_profile).update(
            role=UserRoom.Role.MODERATOR,
            manual_role=UserRoom.Role.MODERATOR,
            synced_role=UserRoom.Role.MODERATOR,
        )

    def tearDown(self):
        cache.clear()

    def test_temporary_mute_requires_reason_for_moderator(self):
        message = Message.objects.create(
            room=lobby_room(), author=self.author_profile, body="bad lobby message"
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
        Message.objects.create(
            room=lobby_room(), author=self.author_profile, body="old bad lobby message"
        )
        RoomMute.objects.create(
            room=self.lobby,
            target=self.author_profile,
            muted_by=self.mod_profile,
            reason="Previous warning",
            expires_at=timezone.now() - timezone.timedelta(days=1),
            duration_days=1,
            revoked_at=timezone.now() - timezone.timedelta(days=1),
        )
        message = Message.objects.create(
            room=lobby_room(), author=self.author_profile, body="new bad lobby message"
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
        self.assertFalse(self.author_profile.mute)
        mute = RoomMute.objects.filter(
            room=self.lobby,
            target=self.author_profile,
            revoked_at__isnull=True,
        ).get()
        self.assertEqual(mute.duration_days, 2)
        self.assertEqual(mute.reason, "Repeated spam")
        # Wall-clock synchronization can move the local clock backwards by a
        # fraction of a second while the request is running.
        self.assertGreaterEqual(
            mute.expires_at,
            before + timezone.timedelta(days=2, seconds=-1),
        )
        self.assertLessEqual(
            mute.expires_at, timezone.now() + timezone.timedelta(days=3)
        )

        notification = Notification.objects.get(owner=self.author_profile)
        self.assertEqual(notification.category, NotificationCategory.CHAT_MUTE)
        self.assertIn("Repeated spam", notification.html_link)
        self.assertEqual(notification.extra_data["type"], "room_mute_notice")
        self.assertEqual(notification.extra_data["reason"], "Repeated spam")
        self.assertTrue(notification.extra_data["mute_until"])

    def test_moderator_cannot_permanently_mute(self):
        message = Message.objects.create(
            room=lobby_room(), author=self.author_profile, body="severe lobby message"
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
        self.assertEqual(response.status_code, 403)

        self.author_profile.refresh_from_db()
        self.assertFalse(self.author_profile.mute)

    def test_permanent_mute_allowed_for_superuser_without_reason(self):
        message = Message.objects.create(
            room=lobby_room(), author=self.author_profile, body="severe lobby message"
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
        notification = Notification.objects.get(owner=self.author_profile)
        self.assertEqual(notification.category, NotificationCategory.CHAT_MUTE)
        self.assertEqual(notification.author, self.admin_profile)
        message.refresh_from_db()
        self.assertFalse(message.hidden)

    def test_site_wide_suspend_can_explicitly_hide_current_channel_messages(self):
        channel = Room.objects.create(
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.CUSTOM,
            name="Moderated channel",
        )
        UserRoom.objects.create(
            room=channel,
            user=self.author_profile,
            role=UserRoom.Role.MEMBER,
            manual_role=UserRoom.Role.MEMBER,
        )
        UserRoom.objects.create(
            room=channel,
            user=self.admin_profile,
            role=UserRoom.Role.ADMIN,
            manual_role=UserRoom.Role.ADMIN,
        )
        first = Message.objects.create(
            room=channel,
            author=self.author_profile,
            body="first channel message",
        )
        second = Message.objects.create(
            room=channel,
            author=self.author_profile,
            body="second channel message",
        )
        lobby_message = Message.objects.create(
            room=self.lobby,
            author=self.author_profile,
            body="unrelated lobby message",
        )
        Room.objects.filter(id=channel.id).update(last_msg_id=second.id)

        self.client.login(username="chatadmin", password="password123")
        response = self.client.post(
            "/chat/mute/",
            {
                "message": second.id,
                "scope": "site",
                "mute_type": "temporary",
                "hide_room_messages": "1",
                "reason": "Channel spam",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.author_profile.refresh_from_db()
        self.assertTrue(self.author_profile.mute)
        self.assertIsNotNone(self.author_profile.mute_until)
        first.refresh_from_db()
        second.refresh_from_db()
        lobby_message.refresh_from_db()
        self.assertTrue(first.hidden)
        self.assertTrue(second.hidden)
        self.assertFalse(lobby_message.hidden)
        channel.refresh_from_db()
        self.assertIsNone(channel.last_msg_id)
        self.assertEqual(
            set(
                ChatModerationLog.objects.filter(message=second).values_list(
                    "action", flat=True
                )
            ),
            {"mute_temp", "hide"},
        )

    def test_site_wide_suspend_is_not_available_from_group(self):
        group = Room.objects.create(room_type=Room.Type.GROUP, name="Private group")
        message = Message.objects.create(
            room=group,
            author=self.author_profile,
            body="private group message",
        )
        self.client.login(username="chatadmin", password="password123")

        response = self.client.post(
            "/chat/mute/",
            {
                "message": message.id,
                "scope": "site",
                "mute_type": "permanent",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.author_profile.refresh_from_db()
        self.assertFalse(self.author_profile.mute)

    def test_site_wide_suspend_requires_active_room_membership(self):
        channel = Room.objects.create(
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.CUSTOM,
            name="Private channel",
        )
        UserRoom.objects.create(
            room=channel,
            user=self.author_profile,
            role=UserRoom.Role.MEMBER,
            manual_role=UserRoom.Role.MEMBER,
        )
        message = Message.objects.create(
            room=channel,
            author=self.author_profile,
            body="private channel message",
        )
        self.client.login(username="chatadmin", password="password123")

        response = self.client.post(
            "/chat/mute/",
            {
                "message": message.id,
                "scope": "site",
                "mute_type": "permanent",
                "hide_room_messages": "1",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")
        self.author_profile.refresh_from_db()
        message.refresh_from_db()
        self.assertFalse(self.author_profile.mute)
        self.assertFalse(message.hidden)


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
                room=lobby_room(),
                author=self.profile,
                body="message %(index)s" % {"index": index},
            )
            for index in range(6)
        ]
        messages[2].hidden = True
        messages[2].save(update_fields=["hidden"])

        view = ChatView()
        view.room_id = lobby_room().id
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
        view.room_id = lobby_room().id

        with CaptureQueriesContext(connection) as queries:
            page = view.get_message_page(last_id=1, page_size=3)

        self.assertEqual(len(queries), 1)
        self.assertEqual(page, [])


class ChatSelfRoomTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            username="selfchat", password="password123", is_staff=True
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

        recent = get_status_context(self.profile)[0]["room_list"]

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


class ChatCommunityPolicyTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        self.problem_group = ProblemGroup.objects.create(
            name="chat-policy", full_name="Chat Policy"
        )
        self.problem = Problem.objects.create(
            code="chatpolicy",
            name="Chat Policy",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=100.0,
            is_public=True,
        )
        self.user = User.objects.create_user(
            username="chatpolicyuser", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    def _post_lobby_message(self, body="hello lobby"):
        self.client.login(username="chatpolicyuser", password="password123")
        with patch("chat_box.views.event.post"):
            return self.client.post(
                "/chat/post/",
                {"room": "", "body": body, "tmp_id": "policy-1"},
            )

    def test_user_without_solve_cannot_post_chat_message(self):
        response = self._post_lobby_message()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "community_access_required")
        self.assertFalse(Message.objects.filter(author=self.profile).exists())

    def test_user_with_full_score_submission_can_post_chat_message(self):
        Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            points=self.problem.points,
            status="D",
            result="AC",
            case_points=self.problem.points,
            case_total=self.problem.points,
        )

        response = self._post_lobby_message()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Message.objects.filter(author=self.profile).exists())


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

        # Create a canonical DM with one message seen only by its author.
        self.room = Room.get_or_create_room(self.profile1, self.profile2)
        message = Message.objects.create(
            room=self.room,
            author=self.profile1,
            body="Unread message",
        )
        self.room.last_msg_id = message.id
        self.room.last_activity_at = message.time
        self.room.save(update_fields=["last_msg_id", "last_activity_at"])
        UserRoom.objects.filter(room=self.room, user=self.profile1).update(
            unread_count=0,
            last_read_message_id=message.id,
        )
        UserRoom.objects.filter(room=self.room, user=self.profile2).update(
            unread_count=1,
            last_read_message_id=None,
        )

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
        response = self.client.post(
            f"/chat/toggle_ignore/{self.profile1.id}",
            {"next": "/"},
        )
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
        user_room2.last_read_message_id = self.room.last_msg_id
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
