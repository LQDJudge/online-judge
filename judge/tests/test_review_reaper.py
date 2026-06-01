from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import Language, Problem, ProblemGroup, Profile
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
        self.assertEqual(review_run.status, ProblemReviewRun.ERROR)

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

    def test_reaped_run_emits_error_notification(self):
        # The reaper must notify author + admins when timing out a run.
        # Without this, the author's dashboard polling JS spins forever
        # because nothing tells them the run was abandoned.
        review_run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.RUNNING,
        )
        ProblemReviewRun.objects.filter(id=review_run.id).update(
            started_at=timezone.now() - timedelta(hours=2)
        )
        with patch("judge.tasks.review._emit_review_error_notifications") as mock_emit:
            result = reap_stale_review_runs()
        self.assertEqual(result["reaped"], 1)
        mock_emit.assert_called_once()
        # The first positional arg should be the ProblemReviewRun instance.
        called_with_run = mock_emit.call_args[0][0]
        self.assertEqual(called_with_run.id, review_run.id)
