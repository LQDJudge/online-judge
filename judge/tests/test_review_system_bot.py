"""Tests for judge.review.system_bot — the auto_review_bot user + comment posting."""

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from judge.models import Comment, Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewRun
from judge.review.system_bot import (
    SYSTEM_BOT_USERNAME,
    get_or_create_system_bot_profile,
    post_system_comment_on_review,
)


class SystemBotProfileTest(TestCase):
    def test_creates_bot_on_first_call(self):
        # No bot user exists initially in a fresh test DB.
        self.assertFalse(User.objects.filter(username=SYSTEM_BOT_USERNAME).exists())
        profile = get_or_create_system_bot_profile()
        self.assertEqual(profile.user.username, SYSTEM_BOT_USERNAME)

    def test_bot_is_inactive_and_unusable_password(self):
        profile = get_or_create_system_bot_profile()
        self.assertFalse(profile.user.is_active)
        self.assertFalse(profile.user.has_usable_password())

    def test_returns_same_profile_on_repeat_calls(self):
        p1 = get_or_create_system_bot_profile()
        p2 = get_or_create_system_bot_profile()
        self.assertEqual(p1.id, p2.id)

    def test_bot_username_is_url_safe(self):
        # The user_page URL regex is \w+ — the bot username must match it,
        # otherwise link_user / reverse('user_page') crash every comment
        # render that includes a bot-authored comment.

        self.assertRegex(SYSTEM_BOT_USERNAME, r"^\w+$")


class PostSystemCommentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )
        user = User.objects.create_user("auth", "a@a.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="sb1",
            name="System Bot",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    def test_no_op_when_no_runs_exist(self):
        # Cannot anchor a comment with no review run for the problem yet.
        comment = post_system_comment_on_review(self.problem, "hello")
        self.assertIsNone(comment)
        self.assertEqual(Comment.objects.filter(body="hello").count(), 0)

    def test_anchors_to_earliest_run(self):
        # When multiple runs exist, the comment lands on the earliest one
        # (the conversation thread anchor — newer runs share the same thread).
        run_old = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="o" * 64,
        )
        run_new = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="n" * 64,
        )
        comment = post_system_comment_on_review(self.problem, "**[System]** test body")
        self.assertIsNotNone(comment)
        self.assertEqual(comment.object_id, run_old.id)
        self.assertNotEqual(comment.object_id, run_new.id)

    def test_author_is_bot_profile(self):
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="o" * 64,
        )
        comment = post_system_comment_on_review(self.problem, "body")
        self.assertEqual(comment.author.user.username, SYSTEM_BOT_USERNAME)

    def test_content_type_is_review_run(self):
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="o" * 64,
        )
        comment = post_system_comment_on_review(self.problem, "body")
        expected_ct = ContentType.objects.get_for_model(ProblemReviewRun)
        self.assertEqual(comment.content_type_id, expected_ct.id)


class ReviewCommentPermissionTest(TestCase):
    """Direct test that non-editors cannot POST comments into a review thread.

    Originally surfaced by code review: the post_comment view did not verify
    write access for ProblemReviewRun targets, letting any authenticated user
    who'd solved one problem post into any private review thread by crafting
    content_type_id + object_id. This test pins the fix.
    """

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )

        author_user = User.objects.create_user("rev_author", "ra@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=author_user, defaults={"language": cls.language}
        )
        # is_staff=True only to bypass the "must have solved 1 problem"
        # anti-spam gate in post_comment — NOT to grant any review/edit
        # rights. is_staff alone does not satisfy is_editable_by, which
        # checks is_superuser OR is_editor. This ensures the test actually
        # reaches the new permission check rather than getting 400'd earlier.
        stranger_user = User.objects.create_user(
            "stranger", "st@x.com", "pw", is_staff=True
        )
        cls.stranger, _ = Profile.objects.get_or_create(
            user=stranger_user, defaults={"language": cls.language}
        )
        admin_user = User.objects.create_user(
            "rev_admin", "ad@x.com", "pw", is_superuser=True, is_staff=True
        )
        cls.admin, _ = Profile.objects.get_or_create(
            user=admin_user, defaults={"language": cls.language}
        )

        cls.problem = Problem.objects.create(
            code="rvp1",
            name="Review perm test",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.author)
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.author,
            input_hash="r" * 64,
        )

    def _post(self, body="hi"):
        ct = ContentType.objects.get_for_model(ProblemReviewRun)
        return self.client.post(
            "/comments/post/",
            {
                "content_type_id": ct.id,
                "object_id": self.review_run.id,
                "body": body,
                "parent": "",
            },
        )

    def test_stranger_cannot_post(self):
        # The headline fix: a logged-in user with no edit rights on this
        # problem must NOT be able to POST into its review thread.
        self.client.force_login(self.stranger.user)
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
