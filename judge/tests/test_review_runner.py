from django.contrib.auth.models import User
from django.test import TestCase
from unittest.mock import patch

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.base import ProblemReviewCheck, CheckResultData
from judge.tasks.review import review_problem


class _DummyCheck(ProblemReviewCheck):
    id = "dummy_pass"
    display_name = "Dummy"

    def run(self, problem, run):
        return CheckResultData(status=ProblemReviewCheckResult.SUCCESS, reason="ok")


class _CrashingCheck(ProblemReviewCheck):
    id = "dummy_crash"
    display_name = "Crash"

    def run(self, problem, run):
        raise ValueError("boom")


class ReviewProblemTaskTest(TestCase):
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
        user = User.objects.create_user("runner", "r@r.com", "x")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rrun1",
            name="Runner test",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    def _make_run(self):
        return ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="h" * 64,
        )

    def test_runs_each_check_and_marks_done(self):
        run = self._make_run()
        with patch("judge.tasks.review.CHECKS", [_DummyCheck()]):
            review_problem(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProblemReviewRun.DONE)
        results = list(run.check_results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].check_id, "dummy_pass")
        self.assertEqual(results[0].status, ProblemReviewCheckResult.SUCCESS)

    def test_crashing_check_becomes_error_row(self):
        run = self._make_run()
        with patch("judge.tasks.review.CHECKS", [_CrashingCheck()]):
            review_problem(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, ProblemReviewRun.DONE)
        results = list(run.check_results.all())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, ProblemReviewCheckResult.ERROR)
        self.assertIn("ValueError", results[0].details_json.get("traceback", ""))

    def test_skipped_check_recorded(self):
        class _Skip(ProblemReviewCheck):
            id = "dummy_skip"
            display_name = "Skip"

            def run(self, problem, run):
                return CheckResultData(
                    status=ProblemReviewCheckResult.SKIPPED, reason="nope"
                )

        run = self._make_run()
        with patch("judge.tasks.review.CHECKS", [_Skip()]):
            review_problem(run.id)
        run.refresh_from_db()
        self.assertEqual(
            run.check_results.first().status, ProblemReviewCheckResult.SKIPPED
        )

    def test_runner_crash_marks_run_error_and_notifies(self):
        # If the registry iteration itself crashes (not a per-check crash
        # which is already handled by the inner try/except), the outer
        # exception wrapper should mark the run ERROR and emit notifications
        # — silent failure leaves the run stuck in RUNNING.
        run = self._make_run()

        # Force a runner-level crash by patching CHECKS to a non-iterable.
        # The exception bubbles past the per-check try/except into the
        # outer wrapper added in this iteration.
        with patch("judge.tasks.review.CHECKS", None), patch(
            "judge.tasks.review._emit_review_error_notifications"
        ) as mock_emit:
            review_problem(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, ProblemReviewRun.ERROR)
        self.assertIsNotNone(run.finished_at)
        mock_emit.assert_called_once()
