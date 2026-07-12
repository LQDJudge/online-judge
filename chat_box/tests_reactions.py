"""Chat message reactions: model constraint + batched summary + react endpoint."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
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
