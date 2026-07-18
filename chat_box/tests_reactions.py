"""Chat message reactions: model constraint + batched summary + react endpoint."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from chat_box.models import Message, MessageReaction, Room
from chat_box.utils import get_reactions_summary
from judge.models import Language, Profile


class ReactionModelTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()  # CacheableModel state isn't rolled back with the DB
        self.lang = Language.objects.first()
        self.u1 = self._profile("react_u1")
        self.u2 = self._profile("react_u2")
        self.msg = Message.objects.create(author=self.u1, body="hi", room=None)

    def tearDown(self):
        cache.clear()

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def test_one_reaction_per_user_per_message(self):
        MessageReaction.objects.create(message=self.msg, user=self.u1, reaction="like")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MessageReaction.objects.create(
                    message=self.msg, user=self.u1, reaction="love"
                )

    def test_summary_counts_and_my_reaction(self):
        MessageReaction.objects.create(message=self.msg, user=self.u1, reaction="love")
        MessageReaction.objects.create(message=self.msg, user=self.u2, reaction="love")
        summary = get_reactions_summary([self.msg.id], self.u1)[self.msg.id]
        self.assertEqual(summary["counts"]["love"], 2)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["my_reaction"], "love")

    def test_summary_empty_for_no_reactions(self):
        summary = get_reactions_summary([self.msg.id], self.u1)[self.msg.id]
        self.assertEqual(summary["total"], 0)
        self.assertIsNone(summary["my_reaction"])

    def test_summary_is_batched_no_nplus1(self):
        # Many messages, each with a reaction -> still a constant number of queries.
        ids = []
        for i in range(10):
            m = Message.objects.create(author=self.u1, body=f"m{i}", room=None)
            MessageReaction.objects.create(message=m, user=self.u2, reaction="haha")
            ids.append(m.id)
        with self.assertNumQueries(2):  # one grouped count + one for my reactions
            get_reactions_summary(ids, self.u1)


class ReactionEndpointTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()  # CacheableModel state isn't rolled back with the DB
        self.lang = Language.objects.first()
        self.u1 = self._profile("er_u1")
        self.u2 = self._profile("er_u2")
        self.u3 = self._profile("er_u3")
        self.lobby_msg = Message.objects.create(author=self.u2, body="hello", room=None)
        self.url = reverse("chat_react")
        self.client.login(username="er_u1", password="pw")

    def tearDown(self):
        cache.clear()

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def _react(self, message, reaction):
        return self.client.post(self.url, {"message": message.id, "reaction": reaction})

    def test_add_reaction(self):
        resp = self._react(self.lobby_msg, "like")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["my_reaction"], "like")
        self.assertEqual(data["counts"]["like"], 1)
        self.assertEqual(
            MessageReaction.objects.filter(message=self.lobby_msg).count(), 1
        )

    def test_change_reaction_replaces_not_adds(self):
        self._react(self.lobby_msg, "like")
        resp = self._react(self.lobby_msg, "love")
        data = resp.json()
        self.assertEqual(
            MessageReaction.objects.filter(message=self.lobby_msg).count(), 1
        )
        self.assertEqual(data["my_reaction"], "love")
        self.assertEqual(data["total"], 1)

    def test_same_reaction_toggles_off(self):
        self._react(self.lobby_msg, "like")
        resp = self._react(self.lobby_msg, "like")
        data = resp.json()
        self.assertEqual(
            MessageReaction.objects.filter(message=self.lobby_msg).count(), 0
        )
        self.assertIsNone(data["my_reaction"])
        self.assertEqual(data["total"], 0)

    def test_invalid_reaction_rejected(self):
        resp = self._react(self.lobby_msg, "bogus")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            MessageReaction.objects.filter(message=self.lobby_msg).count(), 0
        )

    def test_get_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        resp = self._react(self.lobby_msg, "like")
        self.assertEqual(resp.status_code, 302)

    def test_non_member_cannot_react_in_private_room(self):
        room = Room.get_or_create_room(self.u2, self.u3)  # u1 is NOT a member
        msg = Message.objects.create(author=self.u2, body="hi", room_id=room.id)
        resp = self._react(msg, "like")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(MessageReaction.objects.filter(message=msg).count(), 0)

    def test_muted_user_cannot_react(self):
        # Mute u1 (the logged-in reactor) far into the future.
        self.u1.mute = True
        self.u1.mute_until = timezone.now() + timedelta(days=1)
        self.u1.save(update_fields=["mute", "mute_until"])
        resp = self._react(self.lobby_msg, "like")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            MessageReaction.objects.filter(message=self.lobby_msg).count(), 0
        )

    def _broadcast_payloads(self, post_mock):
        return [
            call.args[1]
            for call in post_mock.call_args_list
            if len(call.args) >= 2
            and isinstance(call.args[1], dict)
            and call.args[1].get("type") == "reaction"
        ]

    def test_broadcast_carries_actor_reaction(self):
        with mock.patch("chat_box.views.event.post") as post:
            self._react(self.lobby_msg, "like")
        payloads = self._broadcast_payloads(post)
        self.assertTrue(payloads)
        self.assertEqual(payloads[0]["actor_reaction"], "like")
        self.assertEqual(payloads[0]["user_id"], self.u1.id)

    def test_broadcast_actor_reaction_none_on_toggle_off(self):
        self._react(self.lobby_msg, "like")
        with mock.patch("chat_box.views.event.post") as post:
            self._react(self.lobby_msg, "like")  # same emoji -> toggle off
        payloads = self._broadcast_payloads(post)
        self.assertTrue(payloads)
        self.assertIsNone(payloads[0]["actor_reaction"])


class ReactionListTest(TestCase):
    """The 'who reacted' popup endpoint (chat_reaction_list)."""

    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.lang = Language.objects.first()
        self.u1 = self._profile("rl_u1")
        self.u2 = self._profile("rl_u2")
        self.u3 = self._profile("rl_u3")
        self.msg = Message.objects.create(author=self.u1, body="hi", room=None)
        self.url = reverse("chat_reaction_list")
        self.client.login(username="rl_u1", password="pw")

    def tearDown(self):
        cache.clear()

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def _get(self, message):
        return self.client.get(self.url, {"message": message.id})

    def test_lists_each_reactor_with_emoji(self):
        MessageReaction.objects.create(message=self.msg, user=self.u1, reaction="like")
        MessageReaction.objects.create(message=self.msg, user=self.u2, reaction="love")
        resp = self._get(self.msg)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("rl_u1", html)
        self.assertIn("rl_u2", html)
        self.assertIn("👍", html)
        self.assertIn("❤️", html)
        self.assertIn('class="reaction-list-heading-count">1</span>', html)

    def test_can_filter_reactors_by_reaction_type(self):
        MessageReaction.objects.create(message=self.msg, user=self.u1, reaction="like")
        MessageReaction.objects.create(message=self.msg, user=self.u2, reaction="love")

        resp = self.client.get(self.url, {"message": self.msg.id, "reaction": "like"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("rl_u1", html)
        self.assertNotIn("rl_u2", html)
        self.assertIn("👍", html)
        self.assertNotIn("❤️", html)

    def test_bad_reaction_filter_is_400(self):
        self.assertEqual(
            self.client.get(
                self.url, {"message": self.msg.id, "reaction": "bogus"}
            ).status_code,
            400,
        )

    def test_reactor_list_is_capped_per_reaction_type(self):
        for i in range(12):
            p = self._profile(f"rl_like_{i}")
            MessageReaction.objects.create(message=self.msg, user=p, reaction="like")
        for i in range(3):
            p = self._profile(f"rl_love_{i}")
            MessageReaction.objects.create(message=self.msg, user=p, reaction="love")

        resp = self._get(self.msg)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("rl_like_0", html)
        self.assertIn("rl_like_9", html)
        self.assertNotIn("rl_like_10", html)
        self.assertNotIn("rl_like_11", html)
        self.assertIn("rl_love_0", html)
        self.assertIn("rl_love_2", html)
        self.assertIn("Showing first 10 of 12.", html)

    def test_empty_when_no_reactions(self):
        resp = self._get(self.msg)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("rl_u2", resp.content.decode())

    def test_bad_message_id_is_400(self):
        self.assertEqual(
            self.client.get(self.url, {"message": "9999999"}).status_code, 400
        )

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self._get(self.msg).status_code, 302)

    def test_non_member_cannot_view_list(self):
        room = Room.get_or_create_room(self.u2, self.u3)  # u1 not a member
        msg = Message.objects.create(author=self.u2, body="hi", room_id=room.id)
        MessageReaction.objects.create(message=msg, user=self.u2, reaction="like")
        self.assertEqual(self._get(msg).status_code, 403)

    def test_reactor_list_has_no_nplus1(self):
        def query_count(n, offset):
            m = Message.objects.create(author=self.u1, body=f"m{n}", room=None)
            for i in range(n):
                p = self._profile(f"rln_{offset + i}")
                MessageReaction.objects.create(message=m, user=p, reaction="like")
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(self.url, {"message": m.id})
            return len(ctx.captured_queries)

        # Query count must not grow with the number of reactors.
        self.assertEqual(query_count(2, 0), query_count(12, 100))


class ReactionMessageRenderTest(TestCase):
    """Message HTML should show each reaction type with its own count."""

    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.lang = Language.objects.first()
        self.viewer = self._profile("rm_viewer")
        self.author = self._profile("rm_author")
        self.msg = Message.objects.create(author=self.author, body="hi", room=None)
        self.url = reverse("chat_message_ajax")
        self.client.login(username="rm_viewer", password="pw")

    def tearDown(self):
        cache.clear()

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def test_reaction_summary_counts_each_type_separately(self):
        for i in range(2):
            p = self._profile(f"rm_like_{i}")
            MessageReaction.objects.create(message=self.msg, user=p, reaction="like")
        for i in range(3):
            p = self._profile(f"rm_love_{i}")
            MessageReaction.objects.create(message=self.msg, user=p, reaction="love")

        resp = self.client.get(self.url, {"message": self.msg.id})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(html.count('class="reaction-pill-count"'), 2)
        self.assertIn('class="reaction-pill-count">2</span>', html)
        self.assertIn('class="reaction-pill-count">3</span>', html)
        self.assertNotIn('class="reaction-pill-count">5</span>', html)
