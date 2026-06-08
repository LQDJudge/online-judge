"""solutions_rubric: LLM-driven rubric for ProblemSolutionCode entries.

Authors save reference solutions in the Solution Codes tab (each row has
source_code, language, expected_result, and a last_submission FK populated
when the author clicks "Run"). This check pulls those rows, ignores any
that haven't been run yet, and asks the LLM to grade the set.

Only entries with a completed last_submission (status='D') are sent — without
verdict + timing data the LLM's analysis is much weaker. Authors who haven't
clicked Run get told to run their codes before requesting review.
"""

import json
import logging

from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_data import ProblemSolutionCode
from judge.models.problem_review import ProblemReviewCheckResult
from judge.review.base import ProblemReviewCheck, CheckResultData
from judge.review.llm import LLMCallFailed, call_llm_json
from judge.review.prompts import SOLUTIONS_RUBRIC_SYSTEM

logger = logging.getLogger(__name__)


class SolutionsRubricCheck(ProblemReviewCheck):
    id = "solutions_rubric"
    display_name = gettext_lazy("Solutions")

    def run(self, problem, run):
        all_codes = list(
            ProblemSolutionCode.objects.filter(problem=problem)
            .select_related("language", "last_submission")
            .order_by("order")
        )
        if not all_codes:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "No solution codes saved. Add at least one in the Solution Codes tab "
                    "so the rubric can verify solution correctness, complexity, and "
                    "subtask coverage."
                ),
            )

        # Only entries that have been run (last_submission exists and finished)
        # carry the verdict + timing data the rubric needs. Others get filtered
        # so the LLM isn't asked to grade unjudged source blobs.
        runnable = [
            sc
            for sc in all_codes
            if sc.last_submission is not None and sc.last_submission.status == "D"
        ]
        if not runnable:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "Your solution codes have not been run yet. Open the Solution Codes "
                    "tab and click Run so the rubric can use the verdicts."
                ),
            )

        submission_payloads = []
        for sc in runnable:
            sub = sc.last_submission
            achieved_pct = (
                (sub.case_points / sub.case_total) * 100.0 if sub.case_total else 0.0
            )
            submission_payloads.append(
                {
                    "solution_code_id": sc.id,
                    "name": sc.name or f"Code #{sc.order + 1}",
                    "language": sc.language.name if sc.language else "",
                    "source": sc.source_code,
                    "author_expected_result": sc.expected_result,
                    "actual_result": sub.result or "",
                    "case_points": (
                        float(sub.case_points) if sub.case_points is not None else 0.0
                    ),
                    "case_total": (
                        float(sub.case_total) if sub.case_total is not None else 0.0
                    ),
                    "achieved_pct": round(achieved_pct, 1),
                    "time": float(sub.time) if sub.time else None,
                }
            )

        user_prompt = (
            f"Đề bài:\n{problem.description or ''}\n\n"
            f"Tập bài tham chiếu (JSON):\n"
            f"{json.dumps(submission_payloads, ensure_ascii=False, indent=2)}"
        )

        try:
            report = call_llm_json(SOLUTIONS_RUBRIC_SYSTEM, user_prompt)
        except LLMCallFailed as exc:
            return CheckResultData(
                status=ProblemReviewCheckResult.ERROR,
                reason=_("Solutions rubric LLM call failed: %(error)s")
                % {"error": str(exc)},
            )

        issues = report.get("issues") or []
        if report.get("verdict") == "pass" and not issues:
            return CheckResultData(
                status=ProblemReviewCheckResult.SUCCESS,
                reason=report.get("summary")
                or _("All reference solutions look right."),
                details=report,
            )

        first_issue = issues[0] if issues else _("Solutions rubric flagged an issue.")
        extra = (
            (_(" (+%(more)d more)") % {"more": len(issues) - 1})
            if len(issues) > 1
            else ""
        )
        return CheckResultData(
            status=ProblemReviewCheckResult.FAIL,
            reason=str(first_issue) + extra,
            details=report,
        )
