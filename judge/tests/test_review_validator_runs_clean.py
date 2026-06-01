from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_data import ProblemData
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.checks.validator_runs_clean import ValidatorRunsCleanCheck


class ValidatorRunsCleanTest(TestCase):
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
        user = User.objects.create_user("vr", "vr@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="vr1",
            name="VR",
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

    def test_skip_when_no_problem_data(self):
        # No ProblemData at all → SKIPPED.
        result = ValidatorRunsCleanCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SKIPPED)

    def test_skip_when_no_validator(self):
        # ProblemData exists but no validator configured → SKIPPED with guidance reason.
        ProblemData.objects.create(problem=self.problem, checker="standard")
        result = ValidatorRunsCleanCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SKIPPED)
        self.assertIn("validator", result.reason.lower())
