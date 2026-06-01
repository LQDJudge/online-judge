from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.checks.duplicate_detection import DuplicateDetectionCheck


@override_settings(AUTO_REVIEW_DUPLICATE_THRESHOLD=0.9, USE_ML=True)
class DuplicateDetectionTest(TestCase):
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
        user = User.objects.create_user("dd", "dd@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="dd1",
            name="Dup test",
            description="abc " * 30,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)
        # IMPORTANT: do NOT name this `cls.run` — that shadows TestCase.run.
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.profile,
            input_hash="x" * 64,
        )

    def test_no_similar_problems_passes(self):
        with patch(
            "judge.review.checks.duplicate_detection.similar_problems",
            return_value=[],
        ):
            result = DuplicateDetectionCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SUCCESS)

    def test_high_similarity_match_fails(self):
        fake_matches = [
            {"code": "OTHER", "name": "Other", "score": 0.96, "url": "/problem/OTHER"}
        ]
        with patch(
            "judge.review.checks.duplicate_detection.similar_problems",
            return_value=fake_matches,
        ):
            result = DuplicateDetectionCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertEqual(len(result.details["matches"]), 1)
        self.assertEqual(result.details["matches"][0]["code"], "OTHER")
