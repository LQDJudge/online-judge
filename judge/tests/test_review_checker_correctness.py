from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_data import ProblemData
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.checks.checker_correctness import CheckerCorrectnessCheck


class CheckerCorrectnessTest(TestCase):
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
        user = User.objects.create_user("cc", "cc@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="cc1",
            name="CC",
            description="Print the answer.",
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)
        cls.pd = ProblemData.objects.create(problem=cls.problem, checker="standard")
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.profile,
            input_hash="x" * 64,
        )

    @patch("judge.review.checks.checker_correctness.call_llm_json")
    def test_standard_checker_single_output_passes(self, mock_llm):
        mock_llm.return_value = {
            "multi_output": False,
            "needs_tolerance": False,
            "reason": "single answer",
        }
        result = CheckerCorrectnessCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SUCCESS)

    @patch("judge.review.checks.checker_correctness.call_llm_json")
    def test_standard_checker_with_multi_output_fails(self, mock_llm):
        mock_llm.return_value = {
            "multi_output": True,
            "needs_tolerance": False,
            "reason": "multiple permutations",
        }
        result = CheckerCorrectnessCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        # Language-agnostic: just verify the FAIL produced a non-empty reason.
        self.assertTrue(result.reason)
