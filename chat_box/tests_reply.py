from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import User

from judge.models import Profile
from chat_box.models import Message
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

    def test_missing_parent_is_unavailable(self):
        parent = self._msg("gone")
        child = self._msg("child", reply_to=parent)
        parent.delete()  # SET_NULL clears the link
        child.refresh_from_db()
        # A reply whose parent vanished has reply_to_id=None -> not in quotes.
        self.assertNotIn(child.id, get_reply_quotes([child]))

    def test_no_nplus1_across_many_replies(self):
        parents = [self._msg("p%d" % i) for i in range(5)]
        children = [self._msg("c%d" % i, reply_to=parents[i]) for i in range(5)]

        cache.clear()  # cold Profile cache
        with CaptureQueriesContext(connection) as ctx_many:
            get_reply_quotes(children)

        cache.clear()
        with CaptureQueriesContext(connection) as ctx_one:
            get_reply_quotes(children[:1])

        # Same query count for 5 replies as for 1 -> no per-row query.
        self.assertEqual(len(ctx_many.captured_queries), len(ctx_one.captured_queries))
