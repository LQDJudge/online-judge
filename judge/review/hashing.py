"""
Compute a deterministic content hash of a Problem for the dirty-check guard.

Covers everything the auto-review pipeline reads: statement, time/memory/points,
test data identity + size, checker config, and the set of saved solution codes
(source, language, expected_result, last_submission_id). If a future check
reads a new field, add it here AND add a test in test_review_hashing.py
asserting the hash changes.
"""

import hashlib
import json

from judge.models import Problem
from judge.models.problem_data import ProblemData, ProblemSolutionCode


def _file_field_name(file_field):
    if not file_field:
        return ""
    try:
        return file_field.name or ""
    except (ValueError, AttributeError):
        return ""


def _file_field_size(file_field):
    if not file_field:
        return 0
    try:
        return file_field.size
    except (ValueError, AttributeError, OSError, FileNotFoundError):
        return 0


def _file_field_content_hash(file_field):
    """sha256 of the file's bytes, or "" if unreadable. Used so a custom
    checker swap with the same filename still dirties the input hash."""
    if not file_field:
        return ""
    try:
        with file_field.open("rb") as fh:
            h = hashlib.sha256()
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
            return h.hexdigest()
    except (ValueError, AttributeError, OSError, FileNotFoundError):
        return ""


def compute_input_hash(problem: Problem) -> str:
    payload = {
        "description": problem.description or "",
        "time_limit": float(problem.time_limit) if problem.time_limit else 0.0,
        "memory_limit": int(problem.memory_limit) if problem.memory_limit else 0,
        "points": float(problem.points) if problem.points is not None else 0.0,
        "partial": bool(problem.partial),
    }

    try:
        pd = ProblemData.objects.get(problem=problem)
        payload["test_data_file"] = _file_field_name(pd.zipfile)
        payload["test_data_size"] = _file_field_size(pd.zipfile)
        payload["checker"] = pd.checker or ""
        payload["checker_args"] = pd.checker_args or ""
        # Custom checker: include both filename AND content hash. Filename
        # alone misses the case where an author uploads a different file
        # under the same name; content-only misses renames that may signal
        # an intentional swap. Together they detect both.
        payload["custom_checker_source"] = _file_field_name(pd.custom_checker_cpp)
        payload["custom_checker_content"] = _file_field_content_hash(
            pd.custom_checker_cpp
        )
    except ProblemData.DoesNotExist:
        payload["test_data_file"] = ""
        payload["test_data_size"] = 0
        payload["checker"] = ""
        payload["checker_args"] = ""
        payload["custom_checker_source"] = ""
        payload["custom_checker_content"] = ""

    # Solution codes: source, language, expected verdict, and the linked
    # submission ID. Each Run creates a new Submission row, so including
    # last_submission_id is enough to dirty the hash on re-runs without
    # needing to read submission timing/result directly. Order by `order`
    # (not id) so reordering codes in the UI also dirties the hash —
    # that changes which code is "Code #1" in the rubric prompt.
    codes = ProblemSolutionCode.objects.filter(problem=problem).order_by("order", "id")
    payload["solution_codes"] = [
        {
            "order": sc.order,
            "source": sc.source_code or "",
            "language_id": sc.language_id,
            "expected_result": sc.expected_result or "",
            "last_submission_id": sc.last_submission_id,
        }
        for sc in codes
    ]

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
