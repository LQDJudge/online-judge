"""Snapshot-only evidence jobs. This module never executes author/AI code locally."""

import base64
import hashlib
import io
import json
import logging
import os
import zipfile
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from judge.judgeapi import judge_request
from judge.models.review_evidence import ReviewEvidenceJob
from judge.review.checker_options import valid_builtin_options
from judge.review.artifact_readers import SAFE_ZIP_COMPRESSION
from judge.review.profile_execution import validate_profile_package
from judge.review.signature import SIGNATURE_RUNTIMES
from judge.review.snapshots import read_artifact

OPERATIONS = frozenset(
    (
        "extractor",
        "validator_probe",
        "checker_probe",
        "solution_probe",
        "generator_probe",
        "profile_probe",
    )
)
MAX_SOURCE = 256 * 1024
MAX_CASE_INPUT = 16 * 1024 * 1024
MAX_PACKET = 32 * 1024 * 1024
MAX_CASES = 24
MAX_JOBS = 128
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
logger = logging.getLogger(__name__)


def cancel_evidence_jobs(identities):
    if not identities:
        return
    try:
        judge_request({"name": "cancel-review-evidence", "job-ids": identities})
    except (OSError, ValueError):
        # Site state already rejects late results. Native safety limits still
        # bound execution if the bridge cannot receive immediate cancellation.
        logger.warning("Could not deliver review evidence cancellation")


def payload_digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_payload(operation, payload):
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValueError("Unsupported review evidence operation")
    if "binary_data" in payload and not isinstance(payload["binary_data"], bool):
        raise ValueError("Invalid binary checker metadata")
    source = payload.get("source", {})
    if not isinstance(source, dict) or not isinstance(source.get("source"), str):
        raise ValueError("Evidence source is required")
    raw_source = source["source"].encode()
    if len(raw_source) > MAX_SOURCE:
        raise ValueError("Evidence source exceeds limit")
    if source.get("sha256") != hashlib.sha256(raw_source).hexdigest():
        raise ValueError("Evidence source digest mismatch")
    auxiliaries = source.get("aux_sources", {})
    if not isinstance(auxiliaries, dict) or len(auxiliaries) > 16:
        raise ValueError("Invalid auxiliary source inventory")
    aux_size = 0
    for name, auxiliary in auxiliaries.items():
        if (
            not isinstance(name, str)
            or name != os.path.basename(name)
            or os.path.splitext(name)[1] not in (".h", ".hpp", ".inc")
            or not isinstance(auxiliary, dict)
            or not isinstance(auxiliary.get("source"), str)
        ):
            raise ValueError("Invalid auxiliary source name")
        raw_aux = auxiliary["source"].encode()
        aux_size += len(raw_aux)
        if (
            len(raw_aux) > MAX_SOURCE
            or aux_size > 512 * 1024
            or hashlib.sha256(raw_aux).hexdigest() != auxiliary.get("sha256")
        ):
            raise ValueError("Invalid auxiliary source identity/size")
    language = source.get("language")
    if operation == "profile_probe":
        validate_profile_package(payload.get("package"))
        if language not in set().union(*SIGNATURE_RUNTIMES.values()):
            raise ValueError("Unsupported profile runtime")
    if language == "BUILTIN":
        if (
            operation != "checker_probe"
            or not valid_builtin_options(
                payload.get("builtin_checker"), payload.get("checker_args")
            )
            or raw_source
            or auxiliaries
        ):
            raise ValueError("Invalid built-in checker invocation")
    elif payload.get("builtin_checker") is not None:
        raise ValueError("Built-in checkers require the dedicated invocation mode")
    elif operation != "profile_probe" and language not in (
        "PY3",
        "CPP11",
        "CPP14",
        "CPP17",
        "CPP20",
        "CPP23",
    ):
        raise ValueError("Unsupported evidence language")
    if operation == "extractor" and language != "PY3":
        raise ValueError("Extractors must use Python 3")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise ValueError("Evidence needs bounded nonempty cases")
    ids = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or case["id"] in ids
        ):
            raise ValueError("Evidence cases need unique string identities")
        ids.add(case["id"])
        if language == "BUILTIN" and not all(
            isinstance(case.get(key), str)
            for key in ("expected_output", "candidate_output")
        ):
            raise ValueError("Checker fixtures require both output files")
        for key in ("batch", "position"):
            if key in case and (
                type(case[key]) is not int or not 0 <= case[key] <= 1000000
            ):
                raise ValueError("Invalid probe batch/case position")
        for key in ("input", "expected_output", "candidate_output"):
            if key in case and (
                not isinstance(case[key], str)
                or len(case[key].encode()) > MAX_CASE_INPUT
            ):
                raise ValueError("Evidence case data exceeds limit")
        for encoded_key in ("input_b64", "expected_output_b64", "candidate_output_b64"):
            if encoded_key in case:
                raw = base64.b64decode(case[encoded_key], validate=True)
                if len(raw) > MAX_CASE_INPUT:
                    raise ValueError("Evidence binary fixture exceeds limit")
        if "input" not in case and "input_b64" not in case:
            raise ValueError("Evidence input missing")
    if len(json.dumps(payload, allow_nan=False).encode()) > MAX_PACKET:
        raise ValueError("Evidence packet exceeds limit")
    return payload


def execute_coordinator_evidence(run, operation, payload, attempt_number=1):
    """Schedule once and poll without holding a Celery worker waiting for the judge."""
    validate_payload(operation, payload)
    digest = payload_digest(payload)
    with transaction.atomic():
        locked_run = type(run).objects.select_for_update().get(pk=run.pk)
        if getattr(locked_run, "status", "") in ("C", "CANCELLED"):
            return {"state": "unavailable", "reason": "Review cancelled"}
        existing = ReviewEvidenceJob.objects.filter(
            problem_run=run, operation=operation, payload_hash=digest
        ).first()
        if existing is None:
            if ReviewEvidenceJob.objects.filter(problem_run=run).count() >= MAX_JOBS:
                return {
                    "state": "unavailable",
                    "reason": "Review execution job safety limit reached",
                }
            existing = ReviewEvidenceJob.objects.create(
                problem_run=run,
                operation=operation,
                snapshot_hash=run.content_hash,
                payload_hash=digest,
                payload=payload,
                attempt_number=attempt_number,
            )
            created = True
        else:
            created = False
            timeout = max(
                60, int(getattr(settings, "REVIEW_EVIDENCE_JOB_TIMEOUT_SECONDS", 300))
            )
            origin = existing.started or existing.created
            if existing.status in ("P", "R") and origin < timezone.now() - timedelta(
                seconds=timeout
            ):
                existing.status = "E"
                existing.error = (
                    "Judge evidence job exceeded its queue/execution deadline"
                )
                existing.finished = timezone.now()
                existing.save(update_fields=["status", "error", "finished"])
            if existing.status == "E" and attempt_number > existing.attempt_number:
                existing.attempt_history.append(
                    {
                        "job_id": str(existing.job_id),
                        "number": existing.attempt_number,
                        "result": existing.result,
                        "error": existing.error,
                        "finished": (
                            existing.finished.isoformat() if existing.finished else None
                        ),
                    }
                )
                existing.job_id = uuid.uuid4()
                existing.attempt_number = attempt_number
                existing.status = "P"
                existing.error = ""
                existing.result = {}
                existing.started = existing.finished = None
                existing.created = timezone.now()
                existing.save(
                    update_fields=[
                        "job_id",
                        "attempt_number",
                        "attempt_history",
                        "status",
                        "error",
                        "result",
                        "started",
                        "finished",
                        "created",
                    ]
                )
                created = True
    if created:
        try:
            response = judge_request(
                {"name": "review-evidence-request", "job-id": str(existing.job_id)}
            )
            if response.get("name") != "review-evidence-received":
                raise RuntimeError("Judge bridge declined evidence job")
        except Exception as error:
            ReviewEvidenceJob.objects.filter(pk=existing.pk, status="P").update(
                status="E", error=str(error)[:2000], finished=timezone.now()
            )
        existing.refresh_from_db()
    if existing.status == "D":
        return {
            "state": "done",
            "job_id": str(existing.job_id),
            "result": existing.result,
        }
    if existing.status in ("E", "C"):
        return {
            "state": "unavailable",
            "job_id": str(existing.job_id),
            "reason": existing.error,
            "infrastructure": existing.status == "E",
        }
    return {"state": "pending", "job_id": str(existing.job_id)}


def prepare_extractor_payloads(snapshot, extractor):
    """Read frozen ZIP members without extraction; large/unsupported files stay explicit."""
    source = dict(extractor)
    source["sha256"] = hashlib.sha256(source["source"].encode()).hexdigest()
    rows = [row for row in snapshot.get("tests", []) if row.get("type") == "C"]
    metadata = snapshot.get("artifacts", {}).get("zipfile")
    result = {"payloads": [], "inventory_total": len(rows), "unprocessed": []}
    if not metadata:
        result["unprocessed"] = [
            {
                "id": str(row.get("id", index)),
                "reason": "Frozen generated input bytes unavailable",
            }
            for index, row in enumerate(rows)
        ]
        return result
    with zipfile.ZipFile(io.BytesIO(read_artifact(metadata))) as archive:
        entries = archive.infolist()
        names = {entry.filename: entry for entry in entries}
        duplicate_names = len(names) != len(entries)
        cases = []
        size = 0
        total_size = 0
        for index, row in enumerate(rows):
            identity = str(row.get("id", index))
            entry = names.get(row.get("input_file"))
            if (
                len(result["payloads"]) >= MAX_JOBS
                or total_size + (entry.file_size if entry else 0) > MAX_EXTRACTED_BYTES
            ):
                result["unprocessed"].append(
                    {
                        "id": identity,
                        "reason": "Aggregate extraction input safety limit reached",
                    }
                )
                continue
            if (
                duplicate_names
                or entry is None
                or entry.file_size > MAX_CASE_INPUT
                or entry.flag_bits & 1
                or entry.compress_type not in SAFE_ZIP_COMPRESSION
            ):
                result["unprocessed"].append(
                    {
                        "id": identity,
                        "reason": "Missing, ambiguous, encrypted, oversized or unsupported-compression ZIP input",
                    }
                )
                continue
            with archive.open(entry) as stream:
                raw = stream.read(MAX_CASE_INPUT + 1)
            if len(raw) > MAX_CASE_INPUT:
                result["unprocessed"].append(
                    {"id": identity, "reason": "Input exceeds safety limit"}
                )
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            total_size += len(raw)
            if cases and (
                size + len(encoded) > MAX_PACKET // 2 or len(cases) == MAX_CASES
            ):
                result["payloads"].append({"source": source, "cases": cases})
                cases, size = [], 0
                if len(result["payloads"]) >= MAX_JOBS:
                    result["unprocessed"].append(
                        {
                            "id": identity,
                            "reason": "Extraction job safety limit reached",
                        }
                    )
                    continue
            cases.append(
                {
                    "id": identity,
                    "input_b64": encoded,
                    "input_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            size += len(encoded)
        if cases:
            result["payloads"].append({"source": source, "cases": cases})
    return result
