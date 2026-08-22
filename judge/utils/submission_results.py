import copy
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from judge.utils.storage_helpers import storage_delete_file, storage_file_exists

logger = logging.getLogger(__name__)

RESULT_JSON_VERSION = 1
RESULT_JSON_PREFIX = "submission-results"


def _secret():
    return getattr(settings, "SUBMISSION_RESULT_STORAGE_SECRET", settings.SECRET_KEY)


def submission_result_token(submission_id):
    message = "submission-result:v1:%d" % int(submission_id)
    return hmac.new(
        _secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def submission_result_path(submission_id):
    token = submission_result_token(submission_id)
    return "%s/%s/%s/result.json" % (RESULT_JSON_PREFIX, token[:2], token)


def submission_result_url(submission_id):
    return default_storage.url(submission_result_path(submission_id))


def submission_result_exists(submission_id):
    return storage_file_exists(default_storage, submission_result_path(submission_id))


def delete_submission_result(submission_id):
    storage_delete_file(default_storage, submission_result_path(submission_id))


def _write_exact(path, content):
    storage = default_storage
    if hasattr(storage, "file_overwrite"):
        write_storage = copy.copy(storage)
        if write_storage is not storage:
            write_storage.file_overwrite = True
        else:
            storage_delete_file(storage, path)
    else:
        storage_delete_file(storage, path)
        write_storage = storage

    saved_path = write_storage.save(path, ContentFile(content))
    if saved_path != path:
        storage_delete_file(write_storage, saved_path)
        raise RuntimeError(
            "Submission result JSON saved to unexpected path: %s" % saved_path
        )


def load_submission_result(submission_id):
    path = submission_result_path(submission_id)
    try:
        return _load_submission_result_path(path)
    except FileNotFoundError:
        return _empty_submission_result()
    except Exception:
        logger.exception("Failed to load submission result JSON: %s", path)
        return _empty_submission_result()


def _empty_submission_result():
    return {"version": RESULT_JSON_VERSION, "cases": []}


def _load_submission_result_path(path):
    with default_storage.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def _load_submission_result_for_merge(submission_id):
    path = submission_result_path(submission_id)
    try:
        return _load_submission_result_path(path)
    except FileNotFoundError:
        return _empty_submission_result()


def _normalize_case(case):
    input_value = case.get("input") or ""
    answer_value = case.get("answer") or ""
    return {
        "case": int(case["case"]),
        "input": input_value,
        "input_available": bool(case.get("input_available", "input" in case)),
        "output": case.get("output") or "",
        "answer": answer_value,
        "answer_available": bool(case.get("answer_available", "answer" in case)),
        "feedback": case.get("feedback") or "",
        "extended_feedback": case.get("extended_feedback") or "",
    }


def _compact_case(case):
    compact = {
        "case": int(case["case"]),
        "output": case.get("output") or "",
    }
    if case.get("input_available", "input" in case):
        compact["input"] = case.get("input") or ""
        compact["input_available"] = True
    if case.get("answer_available", "answer" in case):
        compact["answer"] = case.get("answer") or ""
        compact["answer_available"] = True
    if case.get("feedback"):
        compact["feedback"] = case["feedback"]
    if case.get("extended_feedback"):
        compact["extended_feedback"] = case["extended_feedback"]
    return compact


def submission_result_payload(cases, compact=False):
    normalize = _compact_case if compact else _normalize_case
    return {
        "version": RESULT_JSON_VERSION,
        "cases": [normalize(case) for case in sorted(cases, key=lambda c: c["case"])],
    }


def submission_result_content(cases, compact=False):
    return json.dumps(
        submission_result_payload(cases, compact=compact),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def save_submission_result(submission_id, cases):
    path = submission_result_path(submission_id)
    content = submission_result_content(cases)
    _write_exact(path, content)
    return path


def merge_submission_result(submission_id, cases):
    current = _load_submission_result_for_merge(submission_id)
    by_case = {
        int(case["case"]): case
        for case in current.get("cases", [])
        if str(case.get("case", "")).isdigit()
    }
    for case in cases:
        normalized = _normalize_case(case)
        by_case[normalized["case"]] = normalized
    return save_submission_result(submission_id, by_case.values())
