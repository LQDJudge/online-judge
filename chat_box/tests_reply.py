from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.contrib.auth.models import User

from judge.models import Language, Profile
from chat_box.models import Message, Room, UserRoom
from chat_box.views import build_reply_snippet, get_reply_quotes


class ReplyModelTest(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user(username="alice", password="x")
        self.p = Profile.objects.create(user=self.u)

    def tearDown(self):
        cache.clear()

    def test_reply_to_links_parent_and_reverse(self):
        parent = Message.objects.create(author=self.p, body="parent", room=None)
        child = Message.objects.create(
            author=self.p, body="child", room=None, reply_to=parent
        )
        self.assertEqual(child.reply_to_id, parent.id)
        self.assertIn(child, parent.replies.all())

    def test_deleting_parent_nulls_reply_to(self):
        parent = Message.objects.create(author=self.p, body="parent", room=None)
        child = Message.objects.create(
            author=self.p, body="child", room=None, reply_to=parent
        )
        parent.delete()
        child.refresh_from_db()
        self.assertIsNone(child.reply_to_id)


class ReplySnippetTest(TestCase):
    def test_truncates_long_text(self):
        snippet = build_reply_snippet("a" * 200)
        self.assertLessEqual(len(snippet), 61)  # 60 chars + ellipsis
        self.assertTrue(snippet.endswith("…"))

    def test_collapses_whitespace(self):
        self.assertEqual(build_reply_snippet("hello\n\n  world"), "hello world")

    def test_image_only_shows_marker(self):
        self.assertEqual(build_reply_snippet("![](/media/pasted/x.png)"), "[image]")

    def test_short_text_kept_verbatim(self):
        self.assertEqual(build_reply_snippet("thanks!"), "thanks!")


class ReplyQuotesTest(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user(username="bob", password="x")
        self.p = Profile.objects.create(user=self.u)

    def tearDown(self):
        cache.clear()

    def _msg(self, body, reply_to=None, hidden=False):
        return Message.objects.create(
            author=self.p, body=body, room=None, reply_to=reply_to, hidden=hidden
        )

    def test_quote_for_reply(self):
        parent = self._msg("hello parent")
        child = self._msg("child", reply_to=parent)
        quotes = get_reply_quotes([child])
        self.assertEqual(quotes[child.id]["parent_id"], parent.id)
        self.assertEqual(quotes[child.id]["author_id"], self.p.id)
        self.assertEqual(quotes[child.id]["author_name"], "bob")
        self.assertEqual(quotes[child.id]["snippet"], "hello parent")
        self.assertFalse(quotes[child.id]["unavailable"])

    def test_no_quote_for_plain_message(self):
        m = self._msg("plain")
        self.assertNotIn(m.id, get_reply_quotes([m]))

    def test_empty_input_returns_empty(self):
        self.assertEqual(get_reply_quotes([]), {})

    def test_hidden_parent_is_unavailable(self):
        parent = self._msg("secret", hidden=True)
        child = self._msg("child", reply_to=parent)
        self.assertTrue(get_reply_quotes([child])[child.id]["unavailable"])

    def test_cross_room_parent_is_unavailable(self):
        # Defense in depth: post_message blocks cross-room replies, but if a
        # child's FK ever points at a parent in another room (data created
        # outside that path — admin/shell/import, or a later move), rendering
        # must NOT leak the other room's snippet. Re-check room at render time.
        room = Room.objects.create(last_msg_id=None)
        parent = Message.objects.create(author=self.p, body="in room", room=room)
        child = self._msg("child in lobby", reply_to=parent)  # child.room is None
        self.assertTrue(get_reply_quotes([child])[child.id]["unavailable"])

    def test_missing_parent_is_unavailable(self):
        parent = self._msg("gone")
        child = self._msg("child", reply_to=parent)
        parent.delete()  # SET_NULL clears the link
        child.refresh_from_db()
        # A reply whose parent vanished has reply_to_id=None -> not in quotes.
        self.assertNotIn(child.id, get_reply_quotes([child]))

    def test_no_nplus1_across_many_replies(self):
        # Distinct author per parent so the test also guards the public-identity
        # lookup (get_public_username), not just the Profile cache.
        authors = [
            Profile.objects.create(
                user=User.objects.create_user(username="nq%d" % i, password="x")
            )
            for i in range(5)
        ]
        parents = [
            Message.objects.create(author=authors[i], body="p%d" % i, room=None)
            for i in range(5)
        ]
        children = [self._msg("c%d" % i, reply_to=parents[i]) for i in range(5)]

        cache.clear()  # cold Profile + identity caches
        with CaptureQueriesContext(connection) as ctx_many:
            get_reply_quotes(children)

        cache.clear()
        with CaptureQueriesContext(connection) as ctx_one:
            get_reply_quotes(children[:1])

        # Same query count for 5 replies (5 distinct authors) as for 1 -> no
        # per-row and no per-author query.
        self.assertEqual(len(ctx_many.captured_queries), len(ctx_one.captured_queries))


class PostReplyEndpointTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user(
            username="carol", password="pw", is_staff=True
        )
        self.p = Profile.objects.create(user=self.u, language=Language.objects.first())
        self.client.force_login(self.u)

    def tearDown(self):
        cache.clear()

    def _post(self, body, reply_to=""):
        with mock.patch("chat_box.views.event.post"):
            return self.client.post(
                reverse("post_chat_message"),
                {"body": body, "room": "", "reply_to": str(reply_to), "tmp_id": "1"},
            )

    def test_valid_reply_links_parent(self):
        parent = Message.objects.create(author=self.p, body="parent", room=None)
        resp = self._post("child", reply_to=parent.id)
        self.assertEqual(resp.status_code, 200)
        child = Message.objects.latest("id")
        self.assertEqual(child.reply_to_id, parent.id)

    def test_plain_message_has_no_reply(self):
        resp = self._post("hi")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(Message.objects.latest("id").reply_to_id)

    def test_hidden_parent_dropped_but_message_posts(self):
        parent = Message.objects.create(
            author=self.p, body="secret", room=None, hidden=True
        )
        resp = self._post("child", reply_to=parent.id)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(Message.objects.latest("id").reply_to_id)

    def test_nonexistent_parent_dropped_but_message_posts(self):
        resp = self._post("child", reply_to=999999)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(Message.objects.latest("id").reply_to_id)

    def test_cross_room_parent_dropped(self):
        room = Room.objects.create(last_msg_id=None)
        UserRoom.objects.create(user=self.p, room=room)
        parent = Message.objects.create(author=self.p, body="in room", room=room)
        resp = self._post("child in lobby", reply_to=parent.id)  # posts to lobby
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(Message.objects.latest("id").reply_to_id)
