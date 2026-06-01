from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.tasks.review import synthesize_feedback


class SynthesisTest(TestCase):
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
        user = User.objects.create_user("syn", "s@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="syn1",
            name="Syn",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.profile,
            input_hash="x" * 64,
        )
        ProblemReviewCheckResult.objects.create(
            run=cls.review_run,
            check_id="artifacts_present",
            status=ProblemReviewCheckResult.FAIL,
            reason="Missing test_data",
            details_json={"missing": ["test_data"]},
        )

    def test_writes_summary_when_failures_exist(self):
        from judge.tasks import review as rmod

        with patch.object(
            rmod, "_call_llm_text", return_value="**Issues found:** Missing test data."
        ):
            synthesize_feedback(self.review_run.id)
        self.review_run.refresh_from_db()
        self.assertIn("Issues found", self.review_run.summary_report)

    def test_skips_summary_when_all_pass(self):
        # Reset to one PASS result.
        self.review_run.check_results.all().delete()
        ProblemReviewCheckResult.objects.create(
            run=self.review_run,
            check_id="artifacts_present",
            status=ProblemReviewCheckResult.SUCCESS,
            reason="ok",
        )
        from judge.tasks import review as rmod

        with patch.object(
            rmod, "_call_llm_text", return_value="should not be called"
        ) as mock_call:
            synthesize_feedback(self.review_run.id)
        self.review_run.refresh_from_db()
        self.assertEqual(self.review_run.summary_report, "")
        mock_call.assert_not_called()


class NotificationEmissionTest(TestCase):
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
        u = User.objects.create_user("ne", "n@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=u, defaults={"language": cls.language}
        )
        admin = User.objects.create_superuser("ne_admin", "a@x.com", "pw")
        cls.admin, _ = Profile.objects.get_or_create(
            user=admin, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="ne1",
            name="NE",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    def test_emits_to_author_and_admin(self):
        from judge.tasks.review import _emit_review_done_notifications

        run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.DONE,
        )
        with patch("judge.tasks.review.reverse", return_value="/stub/"):
            _emit_review_done_notifications(run)
        from judge.models.notification import Notification

        self.assertGreaterEqual(
            Notification.objects.filter(owner=self.profile).count(), 1
        )
        self.assertGreaterEqual(
            Notification.objects.filter(owner=self.admin).count(), 1
        )
