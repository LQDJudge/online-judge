from django.core.cache import cache
from django.test import TestCase
from django.contrib.auth.models import User

from judge.models import Profile
from chat_box.models import Message


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
