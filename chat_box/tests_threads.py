from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from judge.cache_handler import clear_request_l0_cache
from judge.models import Profile
from chat_box.models import Message, Room, RoomMute, ThreadParticipation, UserRoom
from chat_box.selectors import unread_counts_for_memberships
from chat_box.services.moderation import hide_message
from chat_box.services.unread import mark_room_read
from chat_box.thread_data import thread_summaries
from chat_box.views import ChatView


class ThreadTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        clear_request_l0_cache()
        self.user = User.objects.create_user(username="thread_user", password="pw")
        self.profile = Profile.objects.create(user=self.user)
        self.peer_user = User.objects.create_user(username="thread_peer", password="pw")
        self.peer = Profile.objects.create(user=self.peer_user)
        self.room = Room.objects.create(room_type=Room.Type.GROUP, name="Algorithms")
        self.membership = UserRoom.objects.create(user=self.profile, room=self.room)
        UserRoom.objects.create(user=self.peer, room=self.room)
        self.root = Message.objects.create(
            room=self.room, author=self.peer, body="Root question"
        )
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()
        clear_request_l0_cache()

    def url(self, name="chat_thread", root=None, room=None):
        return reverse(
            name,
            kwargs={"room_id": room or self.room.id, "root_id": root or self.root.id},
        )

    def post_reply(self, body="Thread reply", nonce="nonce-1", **kwargs):
        with patch(
            "chat_box.views.can_use_community_features", return_value=True
        ), patch("chat_box.thread_views.broadcast_room_event"):
            return self.client.post(
                self.url("chat_thread_reply", **kwargs),
                {"body": body, "client_nonce": nonce},
            )

    def reply(self, **kwargs):
        return Message.objects.create(
            room=self.room,
            author=self.peer,
            body="Peer reply",
            thread_root=self.root,
            **kwargs
        )

    def test_reply_saved_without_touching_room_read_cursor_or_preview(self):
        self.room.refresh_from_db()
        self.membership.refresh_from_db()
        original_preview = self.room.last_msg_id
        original_cursor = self.membership.last_read_message_id
        response = self.post_reply()
        self.assertEqual(response.status_code, 200, response.content)
        message = Message.objects.get(pk=response.json()["message"])
        self.assertEqual(message.thread_root_id, self.root.id)
        self.membership.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(self.membership.last_read_message_id, original_cursor)
        self.assertEqual(self.room.last_msg_id, original_preview)
        self.assertTrue(
            ThreadParticipation.objects.get(root=self.root, user=self.profile).following
        )
        self.assertTrue(
            ThreadParticipation.objects.get(root=self.root, user=self.peer).following
        )

    def test_reply_retries_are_idempotent(self):
        first = self.post_reply()
        second = self.post_reply()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["message"], second.json()["message"])
        self.assertEqual(self.root.thread_replies.count(), 1)
        self.assertEqual(self.post_reply(body="Different body").status_code, 400)

    def test_invalid_root_cannot_fall_back_to_channel_message(self):
        other_room = Room.objects.create(room_type=Room.Type.GROUP, name="Other")
        other_root = Message.objects.create(
            room=other_room, author=self.peer, body="Private"
        )
        nested = self.reply()
        system = Message.objects.create(
            room=self.room, kind=Message.Kind.SYSTEM, body="system"
        )
        before = Message.objects.count()
        for root in [other_root.id, nested.id, system.id, 999999999]:
            response = self.post_reply(root=root)
            self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(Message.objects.count(), before)

    def test_revoked_membership_blocks_every_endpoint(self):
        self.membership.state = UserRoom.State.REMOVED
        self.membership.save()
        for name, data in [
            ("chat_thread_read", {"last_reply_id": 1}),
            ("chat_thread_follow", {"following": "true"}),
        ]:
            self.assertEqual(self.client.post(self.url(name), data).status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.post_reply().status_code, 403)
        self.assertEqual(
            self.client.get(reverse("chat_followed_threads")).json()["threads"], []
        )

    def test_archive_and_mute_reject_reply_but_allow_history(self):
        self.room.archived_at = timezone.now()
        self.room.save()
        self.assertEqual(self.post_reply().status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.room.archived_at = None
        self.room.save()
        RoomMute.objects.create(
            room=self.room,
            target=self.profile,
            muted_by=self.peer,
            reason="test",
            duration_days=1,
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        self.assertEqual(self.post_reply().status_code, 403)
        self.assertFalse(self.client.get(self.url()).json()["can_post"])

    def test_thread_replies_are_excluded_from_timeline_and_unread(self):
        reply = self.reply()
        view = ChatView()
        view.room_id = self.room.id
        self.assertEqual(
            [message.id for message in view.get_message_page(2**63 - 1, 50)],
            [self.root.id],
        )
        self.assertEqual(
            unread_counts_for_memberships([self.membership]), {self.room.id: 1}
        )
        membership = mark_room_read(self.room, self.profile)
        self.assertEqual(membership.last_read_message_id, self.root.id)
        self.assertLess(membership.last_read_message_id, reply.id)
        self.assertEqual(
            thread_summaries([self.root], self.profile)[self.root.id]["unread_count"], 1
        )

    def test_thread_read_only_advances_its_own_cursor_monotonically(self):
        first = self.reply()
        last = self.reply()
        self.client.post(self.url("chat_thread_read"), {"last_reply_id": last.id})
        self.client.post(self.url("chat_thread_read"), {"last_reply_id": first.id})
        participation = ThreadParticipation.objects.get(
            root=self.root, user=self.profile
        )
        self.assertEqual(participation.last_read_reply_id, last.id)
        self.membership.refresh_from_db()
        self.assertIsNone(self.membership.last_read_message_id)
        self.assertEqual(
            unread_counts_for_memberships([self.membership]), {self.room.id: 1}
        )
        self.assertEqual(
            thread_summaries([self.root], self.profile)[self.root.id]["unread_count"], 0
        )
        self.assertEqual(
            self.client.post(
                self.url("chat_thread_read"), {"last_reply_id": self.root.id}
            ).status_code,
            400,
        )

    def test_hidden_root_is_redacted_and_read_only(self):
        reply = self.reply()
        self.root.hidden = True
        self.root.save()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["root_hidden"])
        self.assertEqual(response.json()["root_html"], "")
        self.assertNotIn("Root question", response.content.decode())
        self.assertFalse(response.json()["can_post"])
        self.assertEqual(self.post_reply().status_code, 400)
        self.assertEqual(
            self.client.post(
                reverse("chat_react"), {"message": reply.id, "reaction": "like"}
            ).status_code,
            403,
        )

    def test_hidden_reply_not_in_summary_or_rendered_content(self):
        self.reply(hidden=True)
        response = self.client.get(self.url())
        self.assertEqual(response.json()["summary"]["reply_count"], 0)
        self.assertEqual(response.json()["summary"]["unread_count"], 0)
        self.assertNotIn("Peer reply", response.json()["replies_html"])

    def test_detail_renders_root_and_replies_with_separate_dom_ids(self):
        reply = self.reply()
        response = self.client.get(self.url()).json()
        self.assertIn("Root question", response["root_html"])
        self.assertIn("Peer reply", response["replies_html"])
        self.assertIn("thread-message-%s" % self.root.id, response["root_html"])
        self.assertIn("thread-message-%s" % reply.id, response["replies_html"])

    def test_pagination_has_no_gaps_or_duplicates(self):
        Message.objects.bulk_create(
            [
                Message(
                    room=self.room,
                    author=self.peer,
                    thread_root=self.root,
                    body="Reply %s" % index,
                )
                for index in range(35)
            ]
        )
        first = self.client.get(self.url()).json()
        self.assertTrue(first["has_more"])
        second = self.client.get(self.url(), {"before": first["before"]}).json()
        self.assertFalse(second["has_more"])
        self.assertEqual(len(first["reply_ids"]), 30)
        self.assertEqual(len(second["reply_ids"]), 5)
        self.assertFalse(set(first["reply_ids"]) & set(second["reply_ids"]))
        self.assertEqual(first["reply_ids"], sorted(first["reply_ids"]))
        self.assertLess(max(second["reply_ids"]), min(first["reply_ids"]))
        self.assertEqual(
            self.client.get(self.url(), {"before": "oops"}).status_code, 400
        )

    def test_follow_opt_out_survives_more_replies(self):
        self.post_reply()
        self.client.force_login(self.peer_user)
        self.client.post(self.url("chat_thread_follow"), {"following": "false"})
        self.client.force_login(self.user)
        self.post_reply(body="Another reply", nonce="nonce-2")
        self.assertFalse(
            ThreadParticipation.objects.get(root=self.root, user=self.peer).following
        )
        self.assertEqual(
            len(self.client.get(reverse("chat_followed_threads")).json()["threads"]), 1
        )
        self.client.post(self.url("chat_thread_follow"), {"following": "false"})
        self.assertEqual(
            self.client.get(reverse("chat_followed_threads")).json()["threads"], []
        )

    def test_followed_badge_includes_threads_beyond_the_first_page(self):
        for index in range(31):
            root = Message.objects.create(
                room=self.room, author=self.peer, body="Root %s" % index
            )
            ThreadParticipation.objects.create(root=root, user=self.profile)
            Message.objects.create(
                room=self.room, author=self.peer, body="Unread", thread_root=root
            )
        response = self.client.get(reverse("chat_followed_threads")).json()
        self.assertEqual(len(response["threads"]), 30)
        self.assertTrue(response["has_more"])
        self.assertEqual(response["unread_count"], 31)

    def test_legacy_quote_remains_in_channel(self):
        quote = Message.objects.create(
            room=self.room, author=self.profile, body="Legacy quote", reply_to=self.root
        )
        view = ChatView()
        view.room_id = self.room.id
        self.assertIn(
            quote.id, [message.id for message in view.get_message_page(2**63 - 1, 50)]
        )
        self.assertIsNone(quote.thread_root_id)

    def test_root_cannot_be_deleted_or_promote_replies(self):
        self.reply()
        with self.assertRaises(ProtectedError):
            self.root.delete()

    def test_cross_room_corrupt_reply_is_not_exposed(self):
        other_room = Room.objects.create(room_type=Room.Type.GROUP, name="Private")
        Message.objects.create(
            room=other_room,
            author=self.peer,
            body="Other room secret",
            thread_root=self.root,
        )
        response = self.client.get(self.url()).json()
        self.assertNotIn("Other room secret", response["replies_html"])
        self.assertEqual(response["summary"]["reply_count"], 0)

    def test_summary_queries_do_not_grow_per_root(self):
        roots = [
            Message.objects.create(
                room=self.room, author=self.peer, body="Root %s" % index
            )
            for index in range(5)
        ]
        for root in roots:
            Message.objects.create(
                room=self.room, author=self.peer, body="Reply", thread_root=root
            )
        with CaptureQueriesContext(connection) as one:
            thread_summaries(roots[:1], self.profile)
        with CaptureQueriesContext(connection) as many:
            summaries = thread_summaries(roots, self.profile)
        self.assertEqual(len(one), len(many))
        self.assertEqual(len(many), 2)
        self.assertTrue(
            all(summary["reply_count"] == 1 for summary in summaries.values())
        )

    def test_moderation_keeps_room_preview_top_level(self):
        self.post_reply()
        new_root = Message.objects.create(
            room=self.room, author=self.profile, body="Hide me"
        )
        hide_message(new_root, self.user, self.profile)
        self.room.refresh_from_db()
        self.assertEqual(self.room.last_msg_id, self.root.id)
