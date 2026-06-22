from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.models.problem_data import ProblemSolutionCode
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

    def test_fail_when_no_solution_codes(self):
        # Design decision: zero solution codes = FAIL (not SKIPPED). Without
        # any reference the LLM has nothing to grade, which is a hard
        # prerequisite gap the author can fix from the Solution Codes tab.
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertTrue(result.reason)

    def test_fail_when_solution_codes_not_run(self):
        # Saved but never Run: no last_submission → nothing for the LLM to
        # grade against. Should FAIL with a clear "click Run" message.
        ProblemSolutionCode.objects.create(
            problem=self.problem,
            order=0,
            source_code="print('hi')",
            language=self.language,
            expected_result="AC",
        )
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)


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

    def _make_solution_code(
        self, order, expected_result, case_points, case_total, actual_result="AC"
    ):
        sub = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result=actual_result,
            case_points=case_points,
            case_total=case_total,
        )
        return ProblemSolutionCode.objects.create(
            problem=self.problem,
            order=order,
            source_code=f"// solution #{order}",
            language=self.language,
            expected_result=expected_result,
            last_submission=sub,
        )


class OIModeTest(_RubricBase):
    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_oi_missing_subtask_solution_fails(self, mock_llm):
        mock_llm.return_value = {
            "mode": "OI",
            "submissions": [
                {
                    "solution_code_id": 1,
                    "role": "subtask_1",
                    "complexity_observed": "O(N^2)",
                    "correctness": "correct",
                    "achieved_pct": 30.0,
                    "note": "",
                },
                {
                    "solution_code_id": 2,
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
        self._make_solution_code(0, "AC", case_points=30, case_total=100)
        self._make_solution_code(1, "AC", case_points=100, case_total=100)
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
                    "solution_code_id": 1,
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
        self._make_solution_code(0, "TLE", case_points=50, case_total=100)
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("Main AC", result.reason)

    @patch("judge.review.checks.solutions_rubric.call_llm_json")
    def test_icpc_brute_force_aces_all_fails(self, mock_llm):
        mock_llm.return_value = {
            "mode": "ICPC",
            "submissions": [
                {
                    "solution_code_id": 1,
                    "role": "main_ac",
                    "complexity_observed": "O(N log N)",
                    "correctness": "correct",
                    "achieved_pct": 100.0,
                    "note": "",
                },
                {
                    "solution_code_id": 2,
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
        self._make_solution_code(0, "AC", case_points=100, case_total=100)
        self._make_solution_code(1, "TLE", case_points=100, case_total=100)
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
                    "solution_code_id": 1,
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
        self._make_solution_code(0, "AC", case_points=100, case_total=100)
        result = SolutionsRubricCheck().run(self.problem, self.review_run)
        self.assertEqual(result.status, ProblemReviewCheckResult.FAIL)
        self.assertIn("O(N log N)", result.reason)
        self.assertIn("O(N^3)", result.reason)
