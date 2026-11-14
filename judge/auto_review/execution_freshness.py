"""Execution identities from current settings and backend file-group revisions.

No file content is read here. Capture these fingerprints when requesting execution,
and compare them later; never rewrite a finished result's fingerprint after edits.
"""

import hashlib
import json

from django.db.models import F

from judge.models import Problem, ProblemData, ProblemTestCase

TEST_FIELDS = frozenset({"zipfile", "generator", "generator_script"})
VALIDATOR_FIELDS = frozenset({"testcase_validator", "testcase_validator_language"})
JUDGING_FIELDS = frozenset(
    {
        "custom_checker",
        "custom_checker_cpp",
        "interactive_judge",
        "signature_handler",
        "signature_header",
        "communication_manager",
    }
)
REVISION_FIELDS = ("test_revision", "validator_revision", "judging_revision")


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def bump_package_revisions(problem_id, groups):
    fields = {name + "_revision" for name in groups}
    if not fields.issubset(REVISION_FIELDS):
        raise ValueError("Unknown problem file group")
    if fields:
        ProblemData.objects.filter(problem_id=problem_id).update(
            **{name: F(name) + 1 for name in fields}
        )


def bump_review_files(problem_id):
    Problem.objects.filter(pk=problem_id).update(
        review_files_revision=F("review_files_revision") + 1
    )
    Problem.dirty_cache(problem_id)


def _test_configuration(problem, *, validation=False):
    # One package unit, including generator arguments, scoring and test selection.
    fields = (
        ("order", "type", "input_file", "generator_args")
        if validation
        else [
            field.attname
            for field in ProblemTestCase._meta.concrete_fields
            if field.name not in {"id", "dataset"}
        ]
    )
    return list(
        ProblemTestCase.objects.filter(dataset_id=problem.pk)
        .order_by("order", "pk")
        .values(*fields)
    )


def _data(problem):
    return ProblemData.objects.filter(problem_id=problem.pk).values().first() or {}


def validation_execution_hash(problem, *, data=None, tests=None):
    data = _data(problem) if data is None else data
    return digest(
        {
            "test_revision": data.get("test_revision", 0),
            "validator_revision": data.get("validator_revision", 0),
            "validator_language": data.get("testcase_validator_language"),
            "generator_script": data.get("generator_script", ""),
            "tests": (
                _test_configuration(problem, validation=True)
                if tests is None
                else tests
            ),
        }
    )


def solution_package_identity(
    problem, *, data=None, tests=None, language_limits=None, signature_graders=None
):
    data = _data(problem) if data is None else data
    excluded = {
        "id",
        "problem_id",
        "feedback",
        "validator_revision",
        "testcase_validator",
        "testcase_validator_language",
    }
    return {
        "data": {key: value for key, value in data.items() if key not in excluded},
        "tests": _test_configuration(problem) if tests is None else tests,
        "limits": {
            name: getattr(problem, name)
            for name in (
                "time_limit",
                "memory_limit",
                "points",
                "partial",
                "short_circuit",
            )
        },
        "language_limits": (
            list(
                problem.language_limits.order_by("language_id").values(
                    "language_id", "time_limit", "memory_limit"
                )
            )
            if language_limits is None
            else language_limits
        ),
        "signature_graders": (
            list(
                problem.signature_graders.order_by("pk").values(
                    "language", "handler", "header"
                )
            )
            if signature_graders is None
            else signature_graders
        ),
    }


def solution_execution_hash(problem, source, language_id, *, package=None):
    return digest(
        {
            "package": (
                package if package is not None else solution_package_identity(problem)
            ),
            "source": source,
            "language": language_id,
        }
    )


def solution_result_current(problem, solution, submission, *, package=None):
    return bool(
        submission
        and submission.execution_hash
        and submission.execution_hash
        == solution_execution_hash(
            problem, solution.source_code, solution.language_id, package=package
        )
    )
