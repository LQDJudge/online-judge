import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from judge.bridge.django_handler import DjangoHandler
from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import JudgeList
from judge.auto_review.test_statistics import (
    dispatch_extraction,
    extraction_request_id,
    parse_extraction_request_id,
    valid_extraction_result,
)


class TestStatisticsBridgeTests(TestCase):
    def setUp(self):
        self.token = uuid.uuid4()
        self.identity = extraction_request_id(12, self.token)
        self.stage = SimpleNamespace(
            pk=12,
            lease_token=self.token,
            problem_run=SimpleNamespace(problem=SimpleNamespace(code="example")),
        )
        self.request = {
            "problem_code": "example",
            "phase_id": 12,
            "attempt_token": str(self.token),
            "helper_source": "print('{}')",
        }

    def test_request_identity_is_safe_and_round_trips(self):
        self.assertEqual(parse_extraction_request_id(self.identity), (12, self.token))
        self.assertLessEqual(len(self.identity), 64)
        for identity in (
            None,
            [],
            "../../x",
            "stats_12_invalid",
            "stats_0_" + self.token.hex,
            "stats_9999999999999999999_" + self.token.hex,
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                parse_extraction_request_id(identity)

    def test_dispatch_sends_code_and_identity_without_test_bytes(self):
        response = {"name": "test-statistics-received", "request-id": self.identity}
        with (
            patch(
                "judge.auto_review.test_statistics.active_extraction",
                return_value=self.stage,
            ),
            patch(
                "judge.auto_review.test_statistics.judge_request", return_value=response
            ) as send,
        ):
            dispatch_extraction(self.stage, "print('{}')")
        packet = send.call_args.args[0]
        self.assertEqual(packet["request"], self.request)
        self.assertNotIn("cases", packet)

    def test_result_rejects_malformed_or_unbounded_metrics(self):
        result = {
            "protocol": 1,
            "phase_id": 12,
            "attempt_token": str(self.token),
            "status": "completed",
            "cases": [{"name": "1.in", "status": "ok", "metrics": {"n": 3}}],
        }
        self.assertTrue(valid_extraction_result(result, 12, self.token))
        for fields in ({"protocol": True}, {"phase_id": 12.0}, {"phase_id": True}):
            self.assertFalse(
                valid_extraction_result({**result, **fields}, 12, self.token)
            )
        for cases in (
            "not a list",
            ["not an object"],
            [{"name": "1.in", "status": "ok", "metrics": []}],
            [{"name": "1.in", "status": "ok", "metrics": {"n": float("nan")}}],
            [{"name": "1.in", "status": "ok", "metrics": {"text": "x" * 524288}}],
        ):
            with self.subTest(cases=type(cases)):
                self.assertFalse(
                    valid_extraction_result({**result, "cases": cases}, 12, self.token)
                )

    def test_inactive_attempt_is_not_dispatched(self):
        with (
            patch(
                "judge.auto_review.test_statistics.active_extraction", return_value=None
            ),
            patch("judge.auto_review.test_statistics.judge_request") as send,
            self.assertRaises(OSError),
        ):
            dispatch_extraction(self.stage, "print('{}')")
        send.assert_not_called()

    def test_admission_rejects_wrong_problem_token_and_extra_payload(self):
        handler = object.__new__(DjangoHandler)
        handler.judges = Mock()
        for request in (
            {**self.request, "problem_code": "different"},
            {**self.request, "attempt_token": str(uuid.uuid4())},
            {**self.request, "cases": []},
        ):
            with patch(
                "judge.bridge.django_handler.active_extraction", return_value=self.stage
            ):
                result = handler.on_test_statistics_request(
                    {"request-id": self.identity, "request": request}
                )
            self.assertEqual(result["name"], "test-statistics-failed")
        handler.judges.validate.assert_not_called()

    def handler(self):
        handler = object.__new__(JudgeHandler)
        handler._validating = self.identity
        handler.judges = SimpleNamespace(validate_map={self.identity: handler})
        handler._free_self_validation = Mock()
        return handler

    def test_result_requires_actual_judge_reservation(self):
        handler = self.handler()
        handler.judges.validate_map.clear()
        with (
            patch("judge.bridge.judge_handler._ensure_connection"),
            patch("judge.bridge.judge_handler.complete_extraction") as complete,
        ):
            handler.on_test_statistics_result(
                {"request-id": self.identity, "result": {}}
            )
        complete.delay.assert_not_called()
        handler._free_self_validation.assert_not_called()

    def test_result_identity_mismatch_becomes_infrastructure_error(self):
        handler = self.handler()
        result = {
            "protocol": 1,
            "phase_id": 13,
            "attempt_token": str(self.token),
            "status": "completed",
        }
        with (
            patch("judge.bridge.judge_handler._ensure_connection"),
            patch(
                "judge.bridge.judge_handler.active_extraction", return_value=self.stage
            ),
            patch("judge.bridge.judge_handler.complete_extraction") as complete,
        ):
            handler.on_test_statistics_result(
                {"request-id": self.identity, "result": result}
            )
        stage_id, token, accepted = complete.delay.call_args.args
        self.assertEqual((stage_id, token), (12, str(self.token)))
        self.assertEqual(accepted["status"], "infrastructure_error")
        handler._free_self_validation.assert_called_once()

    def test_late_result_does_not_enqueue_completion(self):
        handler = self.handler()
        with (
            patch("judge.bridge.judge_handler._ensure_connection"),
            patch("judge.bridge.judge_handler.active_extraction", return_value=None),
            patch("judge.bridge.judge_handler.complete_extraction") as complete,
        ):
            handler.on_test_statistics_result(
                {"request-id": self.identity, "result": {}}
            )
        complete.delay.assert_not_called()
        handler._free_self_validation.assert_called_once()

    def test_disconnect_fails_only_the_owned_active_attempt(self):
        for owned in (True, False):
            handler = self.handler()
            handler._stop_ping = Mock()
            handler._no_response_job = None
            handler.name = None
            handler.client_address = ("127.0.0.1", 1)
            handler._make_json_log = Mock(return_value={})
            handler.judges.remove = Mock(return_value=(None, {}))
            if not owned:
                handler.judges.validate_map[self.identity] = object()
            with (
                patch("judge.bridge.judge_handler._ensure_connection"),
                patch(
                    "judge.bridge.judge_handler.active_extraction",
                    return_value=self.stage,
                ),
                patch("judge.bridge.judge_handler.complete_extraction") as complete,
            ):
                handler.on_disconnect()
            if owned:
                self.assertEqual(
                    complete.delay.call_args.args[2]["status"], "infrastructure_error"
                )
            else:
                complete.delay.assert_not_called()

    def test_queue_deduplicates_and_cancellation_drops_pending_request(self):
        pool = JudgeList()
        for _ in range(2):
            self.assertTrue(
                pool.validate(
                    self.identity, "example", review=True, request=self.request
                )
            )
        self.assertEqual(len(pool.node_map), 1)
        pool.cancel_statistics([self.identity])
        self.assertFalse(pool.node_map)
