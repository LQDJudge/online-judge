from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.public_request import PublicRequest
from judge.models.problem_review import ProblemReviewRun


@override_settings(AUTO_REVIEW_ENABLED=True, LANGUAGE_CODE="en")
class RequestPublicGuardsTest(TestCase):
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
        user = User.objects.create_user("a", "a@a.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rg1",
            name="Guards",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    def setUp(self):
        self.client.force_login(self.profile.user)

    def _patch_dashboard_reverse(self):
        # The dashboard URL doesn't exist until Task 21; stub reverse so
        # this test runs independently. Patch on the view module.
        return patch("judge.views.internal.reverse", return_value="/stub/")

    def test_first_request_enqueues_run(self):
        with self._patch_dashboard_reverse(), patch(
            "judge.views.internal.review_problem"
        ) as mock_task, self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertEqual(
            ProblemReviewRun.objects.filter(problem=self.problem).count(), 1
        )
        self.assertTrue(PublicRequest.objects.filter(problem=self.problem).exists())
        mock_task.delay.assert_called_once()

    def test_in_flight_blocks_second_request(self):
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.RUNNING,
        )
        with self._patch_dashboard_reverse():
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertFalse(resp.json()["success"])
        self.assertIn("running", resp.json()["error"].lower())

    def test_dirty_check_blocks_when_hash_unchanged(self):
        # The latest run's input_hash equals what compute_input_hash will
        # produce for the unchanged problem state — guard should reject.
        from judge.review.hashing import compute_input_hash

        current_hash = compute_input_hash(self.problem)
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash=current_hash,
            status=ProblemReviewRun.DONE,
        )
        with self._patch_dashboard_reverse():
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertFalse(resp.json()["success"])
        self.assertIn("changes", resp.json()["error"].lower())

    def test_cooldown_blocks_same_user_recent_request(self):
        # Recent run by the same user within the cooldown window → blocked.
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="d" * 64,
            status=ProblemReviewRun.DONE,
        )
        with self._patch_dashboard_reverse():
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        # Expect cooldown response: success=False AND cooldown_seconds_remaining present.
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("cooldown_seconds_remaining", data)
        self.assertGreater(data["cooldown_seconds_remaining"], 0)

    def test_cooldown_bypassed_when_last_run_was_different_user(self):
        # Cooldown only enforces "same author hammering the button". A run
        # triggered by a different profile (e.g. admin re-running) should
        # NOT reset the clock against this author.
        other_user = User.objects.create_user("other", "o@o.com", "pw")
        other_profile, _ = Profile.objects.get_or_create(
            user=other_user, defaults={"language": self.language}
        )
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=other_profile,
            input_hash="d" * 64,
            status=ProblemReviewRun.DONE,
        )
        with self._patch_dashboard_reverse(), patch(
            "judge.views.internal.review_problem"
        ), self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertTrue(resp.json()["success"])

    def test_cooldown_bypassed_for_superuser(self):
        # Admins always bypass cooldown for diagnostic re-runs.
        self.profile.user.is_superuser = True
        self.profile.user.save()
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="d" * 64,
            status=ProblemReviewRun.DONE,
        )
        with self._patch_dashboard_reverse(), patch(
            "judge.views.internal.review_problem"
        ), self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertTrue(resp.json()["success"])

    def test_supersedes_previous_run(self):
        # When a new run is created, the previous latest must have
        # superseded_by set to the new run id.

        # Use a hash that differs from the current problem state.
        prev_run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,  # different from compute_input_hash result
            status=ProblemReviewRun.DONE,
        )
        # Force the started_at backwards so the cooldown guard doesn't trip.
        from django.utils import timezone
        from datetime import timedelta

        ProblemReviewRun.objects.filter(id=prev_run.id).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        with self._patch_dashboard_reverse(), patch(
            "judge.views.internal.review_problem"
        ), self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/internal/request_public",
                {"id": self.problem.id},
            )
        self.assertTrue(resp.json()["success"])
        prev_run.refresh_from_db()
        self.assertIsNotNone(prev_run.superseded_by_id)
