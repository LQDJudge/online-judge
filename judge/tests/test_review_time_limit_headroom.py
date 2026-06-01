from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewRun,
    ProblemReviewSubmissionTag,
)
from judge.review.checks.time_limit_headroom import TimeLimitHeadroomCheck


class TimeLimitHeadroomTest(TestCase):
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
        user = User.objects.create_user("th", "th@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="th1",
            name="TH",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=2.0,
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

    def _make_main_ac(self, time_seconds):
        sub = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="AC",
            case_points=10,
            case_total=10,
            time=time_seconds,
        )
        ProblemReviewSubmissionTag.objects.create(
            submission=sub,
            tagged_by=self.profile,
            kind=ProblemReviewSubmissionTag.MAIN,
            target_subtask=None,
        )
        return sub

    def test_pass_with_headroom(self):
        self._make_main_ac(time_seconds=1.0)  # 50% of 2s TL
        result = TimeLimitHeadroomCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SUCCESS)

    def test_fail_when_too_slow(self):
        self._make_main_ac(time_seconds=1.9)  # 95% of 2s TL > 0.8 default
        result = TimeLimitHeadroomCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)

    def test_skip_when_no_main_ac(self):
        result = TimeLimitHeadroomCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.SKIPPED)
