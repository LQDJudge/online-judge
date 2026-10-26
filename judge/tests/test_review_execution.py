import hashlib
import io
import zipfile
import uuid
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from judge.review.execution import (
    payload_digest,
    execute_coordinator_evidence,
    prepare_extractor_payloads,
    validate_payload,
)
from judge.review.provenance import accept_attestation
from judge.bridge.judge_handler import JudgeHandler


class ReviewExecutionSafetyTests(SimpleTestCase):
    def payload(self):
        source = "print('{}')"
        return {
            "source": {
                "source": source,
                "language": "PY3",
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            },
            "cases": [{"id": "case:1", "input": "1\n"}],
        }

    def test_unsupported_operation_and_language_rejected(self):
        with self.assertRaises(ValueError):
            validate_payload("shell", self.payload())
        payload = self.payload()
        payload["source"]["language"] = "BASH"
        with self.assertRaises(ValueError):
            validate_payload("extractor", payload)

    def test_builtin_checker_mode_is_narrow_and_requires_real_fixtures(self):
        payload = self.payload()
        payload.update(builtin_checker="standard", checker_args={})
        payload["source"] = {
            "source": "",
            "language": "BUILTIN",
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
        payload["cases"][0].update(expected_output="1\n", candidate_output="2\n")
        self.assertIs(validate_payload("checker_probe", payload), payload)
        for operation, update in (
            ("validator_probe", {}),
            ("checker_probe", {"builtin_checker": "bridged"}),
            ("checker_probe", {"builtin_checker": "../../arbitrary"}),
            ("checker_probe", {"checker_args": {"_checker": "injected"}}),
        ):
            with self.subTest(operation=operation, update=update), self.assertRaises(
                ValueError
            ):
                validate_payload(operation, {**payload, **update})
        del payload["cases"][0]["candidate_output"]
        with self.assertRaises(ValueError):
            validate_payload("checker_probe", payload)

    def test_source_digest_and_unique_case_ids_required(self):
        payload = self.payload()
        payload["source"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_payload("extractor", payload)
        payload = self.payload()
        payload["cases"] *= 2
        with self.assertRaises(ValueError):
            validate_payload("extractor", payload)

    def test_extractor_python_only_and_nonempty_suite(self):
        payload = self.payload()
        payload["source"]["language"] = "CPP17"
        with self.assertRaises(ValueError):
            validate_payload("extractor", payload)
        payload["cases"] = []
        with self.assertRaises(ValueError):
            validate_payload("validator_probe", payload)

    def test_digest_stable_and_sensitive_to_actual_input(self):
        payload = self.payload()
        self.assertEqual(
            payload_digest(payload),
            payload_digest(dict(reversed(list(payload.items())))),
        )
        old = payload_digest(payload)
        payload["cases"][0]["input"] = "2\n"
        self.assertNotEqual(old, payload_digest(payload))

    def test_requested_hash_echo_alone_cannot_certify_execution(self):
        requested = {"package_hash": "package", "source_hash": "source"}
        actual = {**requested, "environment": {"judge": "worker"}}
        self.assertFalse(accept_attestation(requested, actual)["environment"])
        actual["artifacts_verified"] = True
        self.assertTrue(accept_attestation(requested, actual)["environment"])
        actual["source_hash"] = "different"
        self.assertFalse(accept_attestation(requested, actual)["environment"])

    def test_zip_extraction_aggregate_budget_preserves_unprocessed_inventory(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(10):
                archive.writestr("%s.in" % index, "1" * 1024)
        snapshot = {
            "tests": [
                {"id": index, "type": "C", "input_file": "%s.in" % index}
                for index in range(10)
            ],
            "artifacts": {"zipfile": {"sha256": "fixture"}},
        }
        with patch(
            "judge.review.execution.read_artifact", return_value=stream.getvalue()
        ), patch("judge.review.execution.MAX_EXTRACTED_BYTES", 2048):
            prepared = prepare_extractor_payloads(snapshot, self.payload()["source"])
        processed = sum(len(payload["cases"]) for payload in prepared["payloads"])
        self.assertEqual(processed, 2)
        self.assertEqual(len(prepared["unprocessed"]), 8)
        self.assertEqual(prepared["inventory_total"], 10)

    def test_generator_inventory_missing_bytes_does_not_disappear(self):
        prepared = prepare_extractor_payloads(
            {
                "tests": [{"id": 1, "type": "C", "generator_args": "100000"}],
                "artifacts": {},
            },
            self.payload()["source"],
        )
        self.assertEqual(prepared["inventory_total"], 1)
        self.assertEqual(len(prepared["unprocessed"]), 1)

    def test_stale_queued_review_uuid_never_runs_ordinary_validator(self):
        handler = SimpleNamespace(_free_self_validation=MagicMock(), send=MagicMock())
        with patch(
            "judge.bridge.judge_handler.ReviewEvidenceJob.objects"
        ) as jobs, patch(
            "judge.bridge.judge_handler.ProblemValidation.objects"
        ) as validations:
            jobs.filter.return_value.first.return_value = None
            JudgeHandler.submit_validate(
                handler, str(uuid.uuid4()), "fixture", review=True
            )
            handler._free_self_validation.assert_called_once()
            handler.send.assert_not_called()
            validations.values_list.assert_not_called()

    def test_lost_job_claim_does_not_launch_duplicate_worker_job(self):
        handler = SimpleNamespace(
            _free_self_validation=MagicMock(), send=MagicMock(), name="worker"
        )
        job = SimpleNamespace(
            pk=1,
            status="P",
            operation="extractor",
            snapshot_hash="snapshot",
            payload_hash="payload",
            payload={},
        )
        with patch("judge.bridge.judge_handler.ReviewEvidenceJob.objects") as jobs:
            jobs.filter.return_value.first.return_value = job
            jobs.filter.return_value.update.return_value = 0
            JudgeHandler.submit_validate(
                handler, str(uuid.uuid4()), "fixture", review=True
            )
            handler._free_self_validation.assert_called_once()
            handler.send.assert_not_called()

    def test_expired_job_requires_new_stage_attempt_and_resets_queue_deadline(self):
        class Run:
            objects = MagicMock()
            pk = 1
            content_hash = "snapshot"
            status = "P"

        run = Run()
        Run.objects.select_for_update.return_value.get.return_value = run
        old_time = timezone.now() - timedelta(hours=1)
        job = SimpleNamespace(
            pk=2,
            job_id=uuid.uuid4(),
            status="P",
            started=None,
            created=old_time,
            finished=None,
            attempt_number=1,
            attempt_history=[],
            result={},
            error="",
            save=MagicMock(),
            refresh_from_db=MagicMock(),
        )
        old_uuid = job.job_id
        with patch(
            "judge.review.execution.transaction.atomic", return_value=nullcontext()
        ), patch("judge.review.execution.ReviewEvidenceJob.objects") as manager, patch(
            "judge.review.execution.judge_request"
        ) as dispatch:
            manager.filter.return_value.first.return_value = job
            result = execute_coordinator_evidence(run, "extractor", self.payload())
            self.assertTrue(result["infrastructure"])
            dispatch.assert_not_called()
            dispatch.return_value = {"name": "review-evidence-received"}
            result = execute_coordinator_evidence(
                run, "extractor", self.payload(), attempt_number=2
            )
            self.assertEqual(result["state"], "pending")
            self.assertNotEqual(job.job_id, old_uuid)
            self.assertGreater(job.created, old_time)
            self.assertEqual(job.attempt_history[0]["job_id"], str(old_uuid))
            dispatch.assert_called_once()
            result = execute_coordinator_evidence(
                run, "extractor", self.payload(), attempt_number=2
            )
            self.assertEqual(result["state"], "pending")
            dispatch.assert_called_once()
