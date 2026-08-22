import copy
import hashlib
import hmac
import json

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from judge.utils.storage_helpers import storage_delete_file

RESULT_JSON_VERSION = 1
RESULT_JSON_PREFIX = "submission-results"
RESULT_TEXT_PREVIEW_MAX_BYTES = 512
ELLIPSIS = "..."


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


def _preview_text(value):
    text = value or ""
    if RESULT_TEXT_PREVIEW_MAX_BYTES <= 0:
        return ""

    data = text.encode("utf-8")
    if len(data) <= RESULT_TEXT_PREVIEW_MAX_BYTES:
        return text

    try:
        return data[:RESULT_TEXT_PREVIEW_MAX_BYTES].decode("utf-8") + ELLIPSIS
    except UnicodeDecodeError as e:
        return data[: e.start].decode("utf-8") + ELLIPSIS


def _normalize_case(case):
    result = {
        "case": int(case["case"]),
        "output": case.get("output") or "",
    }
    if case.get("input_available", "input" in case):
        result["input"] = case.get("input") or ""
        result["input_available"] = True
    if case.get("answer_available", "answer" in case):
        result["answer"] = case.get("answer") or ""
        result["answer_available"] = True
    if case.get("feedback"):
        result["feedback"] = _preview_text(case["feedback"])
    if case.get("extended_feedback"):
        result["extended_feedback"] = _preview_text(case["extended_feedback"])
    return result


def submission_result_payload(cases):
    return {
        "version": RESULT_JSON_VERSION,
        "cases": [
            _normalize_case(case) for case in sorted(cases, key=lambda c: c["case"])
        ],
    }


def submission_result_content(cases):
    return json.dumps(
        submission_result_payload(cases), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def save_submission_result(submission_id, cases):
    path = submission_result_path(submission_id)
    content = submission_result_content(cases)
    _write_exact(path, content)
    return path
