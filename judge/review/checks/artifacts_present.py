"""artifacts_present: statement, >=1 test source, checker key, main AC source uploaded.

`test_data` is considered present if ANY of:
  - `ProblemData.zipfile` (uploaded zip of pre-built test cases), OR
  - `ProblemData.generator` (uploaded generator binary), OR
  - `ProblemData.generator_script` (in-DB generator script text).
LQDOJ supports all three at grading time, so the check accepts any.

`main_ac_source` is considered present if EITHER:
  - The author uploaded a `ProblemSolutionCode` blob via the Solution Codes
    sidebar tab, OR
  - The author tagged at least one Submission as a reference via the
    "Reference solutions for review" panel (`ProblemReviewSubmissionTag`).
"""

from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_data import ProblemData, ProblemSolutionCode
from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewSubmissionTag,
)
from judge.review.base import ProblemReviewCheck, CheckResultData


def _artifact_label(token):
    """Map internal token → user-facing localized label.

    Built per-call (not module load) so gettext sees the active language.
    """
    labels = {
        "statement": _("statement"),
        "test_data": _("test data"),
        "checker": _("checker"),
        "main_ac_source": _("main AC source"),
    }
    return labels.get(token, token)


class ArtifactsPresentCheck(ProblemReviewCheck):
    id = "artifacts_present"
    display_name = gettext_lazy("Artifacts")

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

        # Test data: accept zip OR generator binary OR generator script.
        # Problems vary in which path they use; the judge supports all three.
        has_test_data = pd is not None and (
            (pd.zipfile and pd.zipfile.name)
            or (pd.generator and pd.generator.name)
            or bool((pd.generator_script or "").strip())
        )
        if has_test_data:
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
            # Localize each token so the FAIL reason reads naturally in vi.
            missing_labels = [str(_artifact_label(t)) for t in missing]
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_("Missing: %(missing)s")
                % {"missing": ", ".join(missing_labels)},
                details={"present": present, "missing": missing},
            )
        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("All artifacts uploaded."),
            details={"present": present, "missing": []},
        )
