"""Asynchronous test-statistics dispatch through the existing judge bridge.

Only the judge executes helper code. State and accepted results belong to the
extract_stats phase; there is no second job table or request/response archive.
"""

import json
import logging
import re
import uuid

from judge.judgeapi import judge_request
from judge.models import ReviewStage

logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 256 * 1024
MAX_RESULT_BYTES = 512 * 1024


def extraction_request_id(stage_id, token):
    return "stats_%s_%s" % (stage_id, uuid.UUID(str(token)).hex)


def parse_extraction_request_id(value):
    if not isinstance(value, str):
        raise ValueError("Invalid statistics request identity")
    match = re.fullmatch(r"stats_([1-9][0-9]{0,18})_([0-9a-f]{32})", value)
    if match is None or int(match[1]) >= 2**63:
        raise ValueError("Invalid statistics request identity")
    return int(match[1]), uuid.UUID(hex=match[2])


def active_extraction(stage_id, token):
    return (
        ReviewStage.objects.select_related("problem_run__problem")
        .filter(
            pk=stage_id,
            lease_token=token,
            stage_id="extract_stats",
            status=ReviewStage.RUNNING,
            problem_run__status__in=("R", "B"),
        )
        .first()
    )


def valid_extraction_result(result, stage_id, token):
    if not (
        isinstance(result, dict)
        and type(result.get("protocol")) is int
        and result.get("protocol") == 1
        and result.get("phase_id") == stage_id
        and type(result.get("phase_id")) is int
        and result.get("attempt_token") == str(token)
        and result.get("status")
        in (
            "completed",
            "unavailable",
            "infrastructure_error",
            "cancelled",
            "invalid_request",
        )
    ):
        return False
    cases = result.get("cases", [])
    if not isinstance(cases, list) or len(cases) > 1000:
        return False
    for case in cases:
        if not isinstance(case, dict) or case.get("status") not in (
            "ok",
            "helper_error",
        ):
            return False
        if not isinstance(case.get("name"), str) or len(case["name"]) > 512:
            return False
        if case["status"] == "ok" and not isinstance(case.get("metrics"), dict):
            return False
    try:
        return (
            len(json.dumps(result, allow_nan=False).encode("utf8")) <= MAX_RESULT_BYTES
        )
    except (ValueError, TypeError, RecursionError):
        return False


def dispatch_extraction(stage, helper_source):
    """Return after bridge acceptance; completion arrives independently."""
    if not isinstance(helper_source, str) or not helper_source:
        raise ValueError("Missing statistics helper")
    if len(helper_source.encode("utf8")) > MAX_SOURCE_BYTES:
        raise ValueError("Statistics helper exceeds source limit")
    current = active_extraction(stage.pk, stage.lease_token)
    if current is None:
        raise OSError("Extraction phase is no longer active")
    identity = extraction_request_id(current.pk, current.lease_token)
    request = {
        "problem_code": current.problem_run.problem.code,
        "phase_id": current.pk,
        "attempt_token": str(current.lease_token),
        "helper_source": helper_source,
    }
    try:
        response = judge_request(
            {
                "name": "test-statistics-request",
                "request-id": identity,
                "request": request,
            }
        )
    except (OSError, ValueError) as error:
        raise OSError("Could not dispatch test statistics") from error
    if (
        not isinstance(response, dict)
        or response.get("name") != "test-statistics-received"
        or response.get("request-id") != identity
    ):
        raise OSError("Judge bridge declined test statistics")


def cancel_extractions(identities):
    """Best effort only: cancelled phase/run state rejects any late result."""
    if not identities:
        return
    for offset in range(0, len(identities), 128):
        try:
            judge_request(
                {
                    "name": "cancel-test-statistics",
                    "request-ids": identities[offset : offset + 128],
                }
            )
        except (OSError, ValueError):
            logger.warning("Could not deliver test-statistics cancellation")
