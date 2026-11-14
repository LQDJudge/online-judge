from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import Language, Problem, ProblemGroup, Profile, ReviewStage
from judge.models.problem_review import ProblemReviewRun
from judge.tasks.review import reap_stale_review_runs


class ReaperTest(TestCase):
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
        user = User.objects.create_user("rp", "rp@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rp1",
            name="RP",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    def test_old_running_run_marked_error(self):
        review_run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.RUNNING,
        )
        ProblemReviewRun.objects.filter(id=review_run.id).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        reap_stale_review_runs()
        review_run.refresh_from_db()
        self.assertEqual(review_run.status, ProblemReviewRun.BLOCKED)

    def test_fresh_running_run_left_alone(self):
        review_run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.RUNNING,
        )
        reap_stale_review_runs()
        review_run.refresh_from_db()
        self.assertEqual(review_run.status, ProblemReviewRun.RUNNING)

    def test_reaped_run_requests_author_recovery(self):
        run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status="R",
        )
        ReviewStage.objects.create(
            problem_run=run, stage_id="problem_contract", status="Q"
        )
        ProblemReviewRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        with patch("judge.tasks.coordinator.emit_recovery.delay") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                reap_stale_review_runs()
        run.refresh_from_db()
        self.assertEqual(run.status, "B")
        notify.assert_called_once_with("problem", run.pk)
        stage = run.coordinator_stages.get()
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.error_category, "worker_timeout")
