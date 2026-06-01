from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.checks.solutions_rubric import SolutionsRubricCheck


class SolutionsRubricSkipTest(TestCase):
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
        user = User.objects.create_user("sr", "sr@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="sr1",
            name="SR",
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

    def test_fail_when_no_tags(self):
        # Design decision: no tagged reference submissions = FAIL (not SKIPPED).
        # Without references the LLM has nothing to grade against, which is a
        # hard prerequisite gap the author can fix from the edit page — treating
        # it as SKIPPED would silently hide a configuration problem.
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        # Language-agnostic: just verify there is a non-empty reason.
        self.assertTrue(result.reason)


from unittest.mock import patch

from judge.models import Submission, SubmissionSource
from judge.models.problem_review import ProblemReviewSubmissionTag


class _RubricBase(TestCase):
    """Shared setup for OI/ICPC test classes."""

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
        user = User.objects.create_user("srm", "srm@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="srm1",
            name="SRM",
            description="Subtask 1 (30%) easy. Subtask 2 (70%) hard.",
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=True,
        )
        cls.problem.authors.add(cls.profile)
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.profile,
            input_hash="x" * 64,
        )

    def _make_submission(self, case_points, case_total, result="AC"):
        return Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result=result,
            case_points=case_points,
            case_total=case_total,
        )


class OIModeTest(_RubricBase):
    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_oi_missing_subtask_solution_fails(self, mock_llm):
        # LLM returns OI mode with an issue about missing subtask 2.
        mock_llm.return_value = {
            "mode": "OI",
            "submissions": [
                {
                    "submission_id": 1,
                    "role": "subtask_1",
                    "complexity_observed": "O(N^2)",
                    "correctness": "correct",
                    "achieved_pct": 30.0,
                    "note": "",
                },
                {
                    "submission_id": 2,
                    "role": "main_ac",
                    "complexity_observed": "O(N log N)",
                    "correctness": "correct",
                    "achieved_pct": 100.0,
                    "note": "",
                },
            ],
            "issues": ["Thiếu bài tham chiếu cho subtask 2."],
            "verdict": "fail",
            "summary": "Thiếu bài subtask 2.",
        }
        sub = self._make_submission(case_points=30, case_total=100)
        ProblemReviewSubmissionTag.objects.create(
            submission=sub,
            tagged_by=self.profile,
        )
        main_sub = self._make_submission(case_points=100, case_total=100)
        ProblemReviewSubmissionTag.objects.create(
            submission=main_sub,
            tagged_by=self.profile,
        )
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("subtask 2", result.reason.lower())


class ICPCModeTest(_RubricBase):
    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_icpc_no_main_ac_fails(self, mock_llm):
        mock_llm.return_value = {
            "mode": "ICPC",
            "submissions": [
                {
                    "submission_id": 1,
                    "role": "brute_force",
                    "complexity_observed": "O(N^2)",
                    "correctness": "correct",
                    "achieved_pct": 50.0,
                    "note": "",
                },
            ],
            "issues": ["Thiếu Main AC."],
            "verdict": "fail",
            "summary": "Thiếu bài Main AC.",
        }
        sub = self._make_submission(case_points=50, case_total=100)
        ProblemReviewSubmissionTag.objects.create(
            submission=sub,
            tagged_by=self.profile,
        )
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("Main AC", result.reason)

    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_icpc_brute_force_aces_all_fails(self, mock_llm):
        mock_llm.return_value = {
            "mode": "ICPC",
            "submissions": [
                {
                    "submission_id": 1,
                    "role": "main_ac",
                    "complexity_observed": "O(N log N)",
                    "correctness": "correct",
                    "achieved_pct": 100.0,
                    "note": "",
                },
                {
                    "submission_id": 2,
                    "role": "brute_force",
                    "complexity_observed": "O(N^2)",
                    "correctness": "correct",
                    "achieved_pct": 100.0,
                    "note": "",
                },
            ],
            "issues": ["Brute force đạt full AC — bộ test có thể yếu."],
            "verdict": "fail",
            "summary": "Brute force vẫn AC, test yếu.",
        }
        main_sub = self._make_submission(case_points=100, case_total=100)
        ProblemReviewSubmissionTag.objects.create(
            submission=main_sub,
            tagged_by=self.profile,
        )
        brute_sub = self._make_submission(case_points=100, case_total=100)
        ProblemReviewSubmissionTag.objects.create(
            submission=brute_sub,
            tagged_by=self.profile,
        )
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("brute force", result.reason.lower())


class LLMCorrectnessTest(_RubricBase):
    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_llm_marks_complexity_mismatch(self, mock_llm):
        mock_llm.return_value = {
            "mode": "ICPC",
            "submissions": [
                {
                    "submission_id": 1,
                    "role": "main_ac",
                    "complexity_observed": "O(N^3)",
                    "correctness": "correct",
                    "achieved_pct": 100.0,
                    "note": "Triple loop",
                },
            ],
            "issues": [
                "Khai báo độ phức tạp O(N log N) sai khớp với code O(N^3).",
            ],
            "verdict": "fail",
            "summary": "Complexity không khớp.",
        }
        sub = self._make_submission(case_points=100, case_total=100)
        SubmissionSource.objects.create(submission=sub, source="int main(){}")
        ProblemReviewSubmissionTag.objects.create(
            submission=sub,
            tagged_by=self.profile,
            claimed_complexity="O(N log N)",
        )
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("O(N log N)", result.reason)
        self.assertIn("O(N^3)", result.reason)
