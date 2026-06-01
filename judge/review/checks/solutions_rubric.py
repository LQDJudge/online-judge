"""solutions_rubric: LLM-driven rubric for reference-solution sets.

Authors only attach submission IDs (no kind/role labels). The LLM analyzes the
whole set against the problem statement, classifies each submission's role
(main AC / subtask / brute force), and surfaces overall issues + verdict.
"""

import json
import logging

from django.utils.translation import gettext as _

from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewSubmissionTag,
)
from judge.review.base import ProblemReviewCheck, CheckResultData
from judge.review.llm import LLMCallFailed, call_llm_json
from judge.review.prompts import SOLUTIONS_RUBRIC_SYSTEM

logger = logging.getLogger(__name__)


class SolutionsRubricCheck(ProblemReviewCheck):
    id = "solutions_rubric"
    display_name = "Solutions rubric"

    def run(self, problem, run):
        tags = list(
            ProblemReviewSubmissionTag.objects.filter(
                submission__problem=problem
            ).select_related("submission", "submission__source", "submission__language")
        )
        # No tagged reference submissions: the LLM has nothing to grade against,
        # so the entire rubric layer of validation is impossible. Treat as FAIL
        # (actionable by the author from the edit page) rather than SKIPPED,
        # which would otherwise hide a hard prerequisite gap.
        if not tags:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "No reference submissions attached. Tag at least one on the edit page so the rubric can verify solution correctness, complexity, and subtask coverage."
                ),
            )

        submission_payloads = []
        for tag in tags:
            try:
                source = tag.submission.source.source
            except Exception:
                source = ""
            sub = tag.submission
            achieved_pct = (
                (sub.case_points / sub.case_total) * 100.0 if sub.case_total else 0.0
            )
            submission_payloads.append(
                {
                    "submission_id": sub.id,
                    "language": sub.language.name if sub.language else "",
                    "source": source,
                    "case_points": (
                        float(sub.case_points) if sub.case_points is not None else 0.0
                    ),
                    "case_total": (
                        float(sub.case_total) if sub.case_total is not None else 0.0
                    ),
                    "achieved_pct": round(achieved_pct, 1),
                    "time": float(sub.time) if sub.time else None,
                    "author_hint_kind": tag.kind,
                    "author_hint_target_subtask": tag.target_subtask,
                    "author_hint_complexity": tag.claimed_complexity,
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
