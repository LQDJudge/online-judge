"""Check whether current content is ready for review."""

import hashlib
from datetime import datetime

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from judge.auto_review.checker_options import checker_configurations
from judge.auto_review.problem_types import (
    problem_profile_from_context,
)
from judge.auto_review.profile_config import (
    mismatched_judging_components,
)
from judge.auto_review.signature import (
    enabled_signature_families,
    signature_family,
)
from judge.contest_format import formats


def _bound_evidence(evidence):
    return bool(
        evidence.get("execution_hash")
        and evidence.get("execution_hash") == evidence.get("current_execution_hash")
    )


def preparation_errors(snapshot):
    errors = []
    if snapshot.get("kind") == "contest":
        if not snapshot.get("items"):
            errors.append(_("The contest has no items."))
        config = snapshot.get("config", {})
        try:
            start = datetime.fromisoformat(config.get("start_time", ""))
            end = datetime.fromisoformat(config.get("end_time", ""))
            if end <= start:
                errors.append(_("The contest end must be after its start."))
            duration = (end - start).total_seconds()
            if (
                config.get("time_limit") is not None
                and not 0 < config["time_limit"] <= duration
            ):
                errors.append(
                    _("Contest duration must be positive and fit its window.")
                )
            if (
                config.get("freeze_after") is not None
                and not 0 <= config["freeze_after"] <= duration
            ):
                errors.append(_("The scoreboard freeze must fit the contest window."))
            formats[config.get("format_name")].validate(config.get("format_config", {}))
        except (TypeError, ValueError, KeyError, ValidationError):
            errors.append(
                _("The contest timing, format, or format configuration is invalid.")
            )
        if any(
            (not item.get("problem_id") and not item.get("quiz_id"))
            or item.get("problem_id")
            and item.get("quiz_id")
            or item.get("points", -1) < 0
            for item in snapshot.get("items", [])
        ):
            errors.append(_("Contest item references and points must be valid."))
        return errors
    if (
        not snapshot.get("statement", "").strip()
        and not snapshot.get("pdf_statement", {}).get("text", "").strip()
    ):
        errors.append(_("A readable problem statement is required."))
    data = snapshot.get("data_config", {})
    artifacts = snapshot.get("artifacts", {})
    profile = problem_profile_from_context(snapshot)
    if profile.issues:
        errors.append(
            _(
                "The judging configuration is invalid or conflicts with the saved problem settings. Check Test Data before reviewing."
            )
        )
    elif not profile.review_supported:
        errors.append(
            _(
                "This problem type is not supported by coordinator review yet; ask a superuser for manual review."
            )
        )
    for component in mismatched_judging_components(snapshot):
        label = {
            "checker": _("Checker"),
            "interactor": _("Interactor"),
            "manager": _("Manager"),
            "signature": _("Function / IOI signature"),
        }[component]
        errors.append(
            _(
                "The %(component)s configuration in init.yml does not match Test Data. Save Test Data and run preparation again."
            )
            % {"component": label}
        )
    if profile.signature:
        runtimes = snapshot.get("allowed_language_keys", [])
        families = enabled_signature_families(runtimes)
        rows = [
            row for row in snapshot.get("signature_graders", []) if row.get("active")
        ]
        if (
            not families
            or any(signature_family(key) is None for key in runtimes)
            or any(
                len([row for row in rows if row.get("family") == family]) != 1
                or not artifacts.get("signature:%s:handler" % family)
                or family == "c"
                and not artifacts.get("signature:c:header")
                for family in families
            )
        ):
            errors.append(
                _(
                    "Each enabled signature language family needs exactly one grader and its required interface files."
                )
            )
    if profile.execution == "interactive" and not artifacts.get("interactive_judge"):
        errors.append(
            _("An interactor source is required for this interactive problem.")
        )
    if profile.execution == "communication" and not artifacts.get(
        "communication_manager"
    ):
        errors.append(_("A manager source is required for this communication problem."))
    tests = [row for row in snapshot.get("tests", []) if row.get("type") == "C"]
    if not tests or not (artifacts.get("zipfile") or artifacts.get("generator")):
        errors.append(_("A usable nonempty official testcase package is required."))
    if snapshot.get("inventory", {}).get("errors"):
        errors.append(
            _(
                "The testcase archive has missing or ambiguous configured input/output files."
            )
        )
    if not data.get("checker"):
        errors.append(_("A checker must be configured."))
    checker_names = {row["checker"] for row in checker_configurations(snapshot)} | {
        data.get("checker")
    }
    if "custom" in checker_names and not artifacts.get("custom_checker"):
        errors.append(_("The configured custom checker source is required."))
    elif (
        checker_names.intersection(("customcpp", "testlib", "testlibcms"))
        and not artifacts.get("custom_checker_cpp")
        and not (
            profile.execution == "communication"
            and checker_names.intersection(("customcpp", "testlib", "testlibcms"))
            == {"testlibcms"}
        )
    ):
        errors.append(_("The configured C++ checker source is required."))
    requires_validator = profile.submission != "output" or bool(
        artifacts.get("testcase_validator")
    )
    if requires_validator and not artifacts.get("testcase_validator"):
        errors.append(_("A validator source is required."))
    validation = snapshot.get("validation") or {}
    if requires_validator and not (
        validation.get("status") == "D"
        and validation.get("passed") is True
        and validation.get("total_cases") == len(tests) > 0
        and len(validation.get("cases", [])) == len(tests)
        and all(
            row.get("status") in ("AC", "PASS", "OK")
            for row in validation.get("cases", [])
        )
        and _bound_evidence(validation)
    ):
        errors.append(
            _("Run Validation again and make sure every official input passes.")
        )
    if profile.submission == "output":
        fresh_examples = []
        for example in snapshot.get("output_examples", []):
            evidence = example.get("evidence") or {}

            artifact_hash = (example.get("artifact") or {}).get("revision")
            if (
                evidence.get("status") == "D"
                and not evidence.get("is_pretested")
                and evidence.get("result")
                in (("AC", "WA") if profile.metric else ("AC",))
                and example.get("archive_summary", {}).get("valid") is True
                and len(evidence.get("cases", [])) == len(tests) > 0
                and all(
                    row.get("status") not in (None, "SC", "IE", "AB")
                    for row in evidence.get("cases", [])
                )
                and (
                    profile.metric
                    or evidence.get("case_total", 0) > 0
                    and evidence.get("case_points") == evidence.get("case_total")
                    and all(
                        row.get("status") == "AC"
                        and row.get("points") == row.get("total")
                        for row in evidence.get("cases", [])
                    )
                )
                and _bound_evidence(evidence)
                and artifact_hash
            ):
                fresh_examples.append(example)
        if not fresh_examples:
            errors.append(
                _(
                    "Upload and judge at least one valid current output example on all official tests before reviewing."
                )
            )
        return errors
    solutions = snapshot.get("solutions", [])
    full_ac = False

    if not solutions:
        errors.append(
            _("Add and run Solution Codes, including a full accepted solution.")
        )
    for code in solutions:
        evidence = code.get("evidence") or {}
        source_hash = hashlib.sha256(code.get("source", "").encode()).hexdigest()
        fresh = (
            evidence.get("status") in ("D", "CE")
            and evidence.get("result") not in (None, "IE", "AB")
            and evidence.get("source_hash") == source_hash
            and _bound_evidence(evidence)
        )
        if not fresh:
            errors.append(
                _(
                    "Run Solution Code “%(name)s” again; its result is missing, unfinished, or out of date."
                )
                % {"name": code.get("name") or code["ref"]}
            )
        cases = evidence.get("cases", [])
        if (
            profile.metric
            and fresh
            and evidence.get("status") == "D"
            and not evidence.get("is_pretested")
            and len(cases) == len(tests) > 0
            and all(row.get("status") not in ("SC", "IE", "AB", None) for row in cases)
        ):
            full_ac = True
        if (
            fresh
            and code.get("expected_result") == "AC"
            and evidence.get("result") == "AC"
            and not evidence.get("is_pretested")
            and len(cases) == len(tests) > 0
            and all(
                row.get("status") == "AC" and row.get("points") == row.get("total")
                for row in cases
            )
            and evidence.get("case_total", 0) > 0
            and evidence.get("case_points") == evidence.get("case_total")
        ):
            full_ac = True
    if not full_ac and profile.metric:
        errors.append(
            _(
                "Run at least one current scoring submission on every official testcase. A perfect score is not required."
            )
        )
    elif not full_ac:
        errors.append(
            _(
                "At least one full accepted solution must pass every official test with current execution results."
            )
        )
    return errors
