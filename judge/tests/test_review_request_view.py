from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.public_request import PublicRequest
from judge.models.problem_review import ProblemReviewRun


@override_settings(LANGUAGE_CODE="en")
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
        # Request Public now requires the setter qualification (judge.add_problem),
        # the same gate used to create/publish problems.
        user.user_permissions.add(Permission.objects.get(codename="add_problem"))
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

    def test_request_requires_setter_qualification(self):
        editor = User.objects.create_user("unqualified", password="test")
        profile, _ = Profile.objects.get_or_create(
            user=editor, defaults={"language": self.language}
        )
        self.problem.authors.add(profile)
        self.client.force_login(editor)
        response = self.client.post("/internal/request_public", {"id": self.problem.pk})
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.status_code, 403)

    def test_request_does_not_start_ai_when_no_completed_review(self):
        with patch("judge.auto_review.scheduling.current_app.send_task") as send:
            response = self.client.post(
                "/internal/request_public", {"id": self.problem.pk}
            )
        self.assertFalse(response.json()["success"])
        self.assertFalse(PublicRequest.objects.exists())
        self.assertFalse(ProblemReviewRun.objects.exists())
        send.assert_not_called()

    def test_completed_current_report_creates_request_without_rerunning_review(self):
        run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            status="D",
            readiness="READY",
            input_hash="current",
        )
        with patch(
            "judge.auto_review.workflow.publication_eligibility", return_value=True
        ), patch(
            "judge.auto_review.workflow.review_is_current", return_value=True
        ), patch(
            "judge.auto_review.scheduling.current_app.send_task"
        ) as send:
            response = self.client.post(
                "/internal/request_public", {"id": self.problem.pk, "run_id": run.pk}
            )
        self.assertTrue(response.json()["success"])
        self.assertEqual(PublicRequest.objects.get().review_run_id, run.pk)
        send.assert_not_called()
