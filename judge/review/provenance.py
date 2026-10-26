"""Evidence provenance captured before execution and independently attested by judges."""

import hashlib
import logging

from judge.review.snapshots import SnapshotError, build_problem_manifest
from judge.review.artifact_readers import MAX_OUTPUT_EXAMPLE_BYTES, open_output_source
from judge.review.snapshots import freeze_file

logger = logging.getLogger(__name__)


def prepared_binding(problem, source_hash):
    manifest = build_problem_manifest(problem, include_execution=False)
    relevant = (
        "init_yml",
        "zipfile",
        "generator",
        "testcase_validator",
        "custom_checker_cpp",
        "custom_checker",
        "interactive_judge",
        "communication_manager",
    )
    artifacts = {
        key: manifest["artifacts"][key]
        for key in relevant
        if manifest["artifacts"].get(key)
    }
    artifacts.update(
        {
            key: value
            for key, value in manifest["artifacts"].items()
            if key.startswith(("aux:", "signature:")) and value
        }
    )
    return {
        "content_hash": manifest["content_hash"],
        "package_hash": manifest["package_hash"],
        "source_hash": source_hash,
        "requested_artifacts": artifacts,
        "expected_cases": sum(row.get("type") == "C" for row in manifest["tests"]),
        "environment": {},
        "protocol": 1,
    }


def solution_binding(problem, source, *, output_artifact=False):
    try:
        binding = prepared_binding(problem, hashlib.sha256(source.encode()).hexdigest())
        if output_artifact:
            with open_output_source(source) as stream:
                binding["output_artifact_sha256"] = freeze_file(
                    stream, persist=False, max_bytes=MAX_OUTPUT_EXAMPLE_BYTES
                )["sha256"]
            binding["output_portfolio"] = True
        return binding
    except (SnapshotError, OSError, ValueError) as error:
        logger.warning(
            "Could not capture solution evidence for problem %s: %s",
            problem.pk,
            type(error).__name__,
        )
        return {
            "environment": {},
            "attestation_error": "Package provenance unavailable",
        }


def validator_binding(problem):
    try:
        manifest = build_problem_manifest(problem, include_execution=False)
        metadata = manifest["artifacts"].get("testcase_validator") or {}
        return prepared_binding(problem, metadata.get("sha256", ""))
    except (SnapshotError, OSError, ValueError) as error:
        logger.warning(
            "Could not capture validator evidence for problem %s: %s",
            problem.pk,
            type(error).__name__,
        )
        return {
            "environment": {},
            "attestation_error": "Package provenance unavailable",
        }


def accept_attestation(requested, actual):
    """No environment is trusted unless the worker actually verified requested bytes."""
    if not isinstance(actual, dict) or not actual.get("artifacts_verified"):
        return {
            **requested,
            "environment": {},
            "attestation_error": "Missing or mismatched judge artifact attestation",
        }
    if actual.get("package_hash") != requested.get("package_hash") or actual.get(
        "source_hash"
    ) != requested.get("source_hash"):
        return {
            **requested,
            "environment": {},
            "attestation_error": "Judge provenance identity mismatch",
        }
    if (
        requested.get("output_artifact_sha256")
        and actual.get("output_artifact_sha256") != requested["output_artifact_sha256"]
    ):
        return {
            **requested,
            "environment": {},
            "attestation_error": "Output artifact bytes were not verified by the judge",
        }
    return {
        **requested,
        "environment": actual.get("environment", {}),
        "worker_attestation": actual,
    }
