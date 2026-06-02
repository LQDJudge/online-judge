"""validator_runs_clean: confirm a testcase validator is configured.

The existing judge bridge handles actual validator execution: it compiles C++
validators, interprets Python validators, and runs them against every test
case on each contestant submission. Our review check therefore only verifies
presence here; broken validators surface naturally when contestants submit.

A follow-up feature may generate validators via LLM when missing and integrate
saving into the same `ProblemDataCompiler.generate()` flow the manual upload
path uses. Deferred from v1 — see `VALIDATOR_GENERATION_SYSTEM` in
`judge/review/prompts.py` for the prompt skeleton.
"""

from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_data import ProblemData, ProblemValidation
from judge.models.problem_review import ProblemReviewCheckResult
from judge.review.base import CheckResultData, ProblemReviewCheck


class ValidatorRunsCleanCheck(ProblemReviewCheck):
    id = "validator_runs_clean"
    display_name = gettext_lazy("Validator")

    def run(self, problem, run):
        try:
            pd = ProblemData.objects.get(problem=problem)
        except ProblemData.DoesNotExist:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_("No problem data configured."),
            )

        validator_present = ProblemValidation.objects.filter(problem=problem).exists()
        testcase_validator = getattr(pd, "testcase_validator", None)
        if testcase_validator and getattr(testcase_validator, "name", ""):
            validator_present = True

        if not validator_present:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_(
                    "No validator configured — add one in problem data to enable this check. "
                    "A validator is recommended for high-quality CP problems."
                ),
            )

        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("Validator is configured."),
            details={"validator_present": True},
        )
