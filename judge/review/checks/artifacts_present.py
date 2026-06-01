"""artifacts_present: statement, >=1 test, checker key, main AC source uploaded.

`main_ac_source` is considered present if EITHER:
  - The author uploaded a `ProblemSolutionCode` blob via the Solution Codes
    sidebar tab, OR
  - The author tagged at least one Submission as a reference via the
    "Reference solutions for review" panel (`ProblemReviewSubmissionTag`).
Both surfaces capture "we have a reference AC for this problem" from
slightly different angles; either is sufficient evidence.
"""

from django.conf import settings
from django.utils.translation import gettext as _

from judge.models.problem_data import ProblemData, ProblemSolutionCode
from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewSubmissionTag,
)
from judge.review.base import ProblemReviewCheck, CheckResultData


class ArtifactsPresentCheck(ProblemReviewCheck):
    id = "artifacts_present"
    display_name = "Artifacts present"

    def run(self, problem, run):
        min_chars = getattr(settings, "AUTO_REVIEW_STATEMENT_MIN_CHARS", 100)
        present = []
        missing = []

        if (problem.description or "").strip() and len(
            problem.description
        ) >= min_chars:
            present.append("statement")
        else:
            missing.append("statement")

        try:
            pd = ProblemData.objects.get(problem=problem)
        except ProblemData.DoesNotExist:
            pd = None

        if pd and pd.zipfile and pd.zipfile.name:
            present.append("test_data")
        else:
            missing.append("test_data")

        if pd and pd.checker:
            present.append("checker")
        else:
            missing.append("checker")

        has_solution_code = ProblemSolutionCode.objects.filter(problem=problem).exists()
        has_reference_tag = ProblemReviewSubmissionTag.objects.filter(
            submission__problem=problem
        ).exists()
        if has_solution_code or has_reference_tag:
            present.append("main_ac_source")
        else:
            missing.append("main_ac_source")

        if missing:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_("Missing: %(missing)s") % {"missing": ", ".join(missing)},
                details={"present": present, "missing": missing},
            )
        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("All artifacts uploaded."),
            details={"present": present, "missing": []},
        )
