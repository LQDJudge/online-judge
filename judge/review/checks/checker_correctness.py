"""checker_correctness: validates default-vs-custom checker choice + custom checker validity."""

import logging

from django.utils.translation import gettext as _

from judge.models.problem_data import ProblemData
from judge.models.problem_review import ProblemReviewCheckResult
from judge.review.base import ProblemReviewCheck, CheckResultData
from judge.review.llm import LLMCallFailed, call_llm_json
from judge.review.prompts import CHECKER_MULTIOUTPUT_SYSTEM, CHECKER_VALIDITY_SYSTEM

logger = logging.getLogger(__name__)


class CheckerCorrectnessCheck(ProblemReviewCheck):
    id = "checker_correctness"
    display_name = "Checker correctness"

    def run(self, problem, run):
        try:
            pd = ProblemData.objects.get(problem=problem)
        except ProblemData.DoesNotExist:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_("No ProblemData configured."),
            )

        checker_key = (pd.checker or "").lower()

        # Branch 2: custom checker — validate the source.
        if checker_key in ("custom", "customval", "customcpp"):
            return self._validate_custom_checker(problem, pd)

        # Branch 1: detect multi-output via LLM.
        try:
            multi = call_llm_json(
                CHECKER_MULTIOUTPUT_SYSTEM,
                f"Problem statement:\n\n{problem.description or ''}",
            )
        except LLMCallFailed as exc:
            return CheckResultData(
                status=ProblemReviewCheckResult.ERROR,
                reason=_("Multi-output detection failed: %(error)s") % {"error": exc},
            )

        if multi.get("multi_output") and checker_key == "standard":
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "Statement implies multiple valid outputs but the default checker is used. Add a custom checker."
                ),
                details={"branch": "multi_output", "llm_response": multi},
            )

        if (
            multi.get("multi_output")
            and checker_key == "floats"
            and multi.get("needs_tolerance")
        ):
            return CheckResultData(
                status=ProblemReviewCheckResult.SUCCESS,
                reason=_(
                    "Floating-point checker matches statement's tolerance requirement."
                ),
                details={"branch": "multi_output", "llm_response": multi},
            )

        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("Checker '%(key)s' is appropriate for this problem.")
            % {"key": checker_key},
            details={"branch": "standard_ok", "llm_response": multi},
        )

    def _validate_custom_checker(self, problem, pd):
        # Best-effort read of custom checker source. Field names vary; try several.
        source = ""
        for field_name in ("custom_checker_cpp", "custom_checker", "checker"):
            value = getattr(pd, field_name, None)
            if value is None or value == "":
                continue
            # It might be a FieldFile (open and read) or a plain text field.
            try:
                if hasattr(value, "read"):
                    value.open("r")
                    source = value.read()
                    value.close()
                else:
                    source = str(value)
                if source:
                    break
            except Exception:
                continue

        if not source:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_("Checker is set to custom but no checker source is uploaded."),
                details={"branch": "custom_validity"},
            )

        try:
            report = call_llm_json(
                CHECKER_VALIDITY_SYSTEM,
                f"Statement:\n{problem.description or ''}\n\nChecker source:\n```\n{source}\n```",
            )
        except LLMCallFailed as exc:
            return CheckResultData(
                status=ProblemReviewCheckResult.ERROR,
                reason=_("Custom checker validation failed: %(error)s")
                % {"error": exc},
            )

        if report.get("verdict") == "ok":
            return CheckResultData(
                status=ProblemReviewCheckResult.SUCCESS,
                reason=_("Custom checker appears correct."),
                details={"branch": "custom_validity", "llm_response": report},
            )
        return CheckResultData(
            status=ProblemReviewCheckResult.FAIL,
            reason=_("Custom checker issues: %(reason)s")
            % {"reason": report.get("reason", "unspecified")},
            details={"branch": "custom_validity", "llm_response": report},
        )
