from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewRun,
    ProblemReviewSubmissionTag,
)
from judge.review.checks.artifacts_present import ArtifactsPresentCheck


class ArtifactsPresentTest(TestCase):
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
        user = User.objects.create_user("ap", "ap@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="ap1",
            name="Artifacts",
            description="",  # empty
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

    def test_empty_problem_fails_with_all_missing(self):
        result = ArtifactsPresentCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        missing = result.details["missing"]
        self.assertIn("statement", missing)
        self.assertIn("test_data", missing)
        self.assertIn("checker", missing)
        self.assertIn("main_ac_source", missing)

    def test_statement_too_short_marked_missing(self):
        self.problem.description = "short"
        self.problem.save()
        result = ArtifactsPresentCheck().run(self.problem, self.review_run)
        self.assertIn("statement", result.details["missing"])

    def test_tagged_submission_satisfies_main_ac_source(self):
        # When the author has tagged at least one Submission as a reference
        # (even without a ProblemSolutionCode blob), main_ac_source should
        # count as present.
        sub = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="AC",
            case_points=10,
            case_total=10,
        )
        ProblemReviewSubmissionTag.objects.create(
            submission=sub,
            tagged_by=self.profile,
        )
        result = ArtifactsPresentCheck().run(self.problem, self.review_run)
        self.assertIn("main_ac_source", result.details["present"])
        self.assertNotIn("main_ac_source", result.details["missing"])
