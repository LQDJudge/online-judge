import errno
import json
import zlib
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from django.db import OperationalError

from judge.bridge.base_handler import Disconnect, ZlibPacketHandler
from judge.bridge.django_handler import DjangoHandler
from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import JudgeList
from judge.tasks.submission import save_submission_result_details


class FakeSocket:
    def __init__(self, chunks=None, fail_shutdown=False):
        self.chunks = list(chunks or [])
        self.fail_shutdown = fail_shutdown
        self.closed = False
        self.shutdowns = 0

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def shutdown(self, flags):
        self.shutdowns += 1
        if self.fail_shutdown or self.shutdowns > 1:
            raise OSError("already closed")

    def close(self):
        self.closed = True


class BadFdSocket(FakeSocket):
    def recv(self, size):
        raise OSError(errno.EBADF, "Bad file descriptor")


class PacketHandlerForTest(ZlibPacketHandler):
    def on_packet(self, data):
        self.packet = data


def make_packet_handler(sock):
    handler = object.__new__(PacketHandlerForTest)
    handler.request = sock
    handler.server = None
    handler.client_address = ("127.0.0.1", 1)
    handler.server_address = ("127.0.0.1", 2)
    handler._initial_tag = None
    handler._got_packet = False
    return handler


class FakeJudge:
    def __init__(
        self,
        name="judge",
        problems=("aplusb",),
        languages=("PY3",),
        fail_submit=False,
        fail_disconnect=False,
        assert_unlocked=None,
        load=0,
    ):
        self.name = name
        self.problems = set(problems)
        self.languages = set(languages)
        self.fail_submit = fail_submit
        self.fail_disconnect = fail_disconnect
        self.assert_unlocked = assert_unlocked
        self.load = load
        self.working = False
        self._working = False
        self._working_data = {}
        self._validating = None
        self._validating_problem = None
        self.executors = {language: [] for language in self.languages}
        self.latency = None
        self.judge_address = None
        self.client_address = ("127.0.0.1", 9999)
        self.submitted = []
        self.disconnected = False
        self.sent = []

    def can_judge(self, problem, language, judge_id):
        if judge_id and judge_id != self.name:
            return False
        return problem in self.problems and language in self.languages

    def submit(self, id, problem, language, source):
        if self.assert_unlocked:
            self.assert_unlocked()
        if self.fail_submit:
            raise OSError("broken pipe")
        self.working = True
        self._working = id
        self._working_data = {
            "problem": problem,
            "language": language,
            "source": source,
        }
        self.submitted.append(id)

    def submit_validate(self, validate_id, problem_id):
        if self.fail_submit:
            raise OSError("broken pipe")
        self.working = True
        self._working = True
        self._validating = validate_id
        self._validating_problem = problem_id

    def get_current_submission(self):
        return self._working if self._working is not True else None

    def abort(self):
        if self.assert_unlocked:
            self.assert_unlocked()
        self.sent.append({"name": "terminate-submission"})

    def disconnect(self, force=False):
        if self.assert_unlocked:
            self.assert_unlocked()
        if self.fail_disconnect:
            raise OSError("broken pipe")
        self.disconnected = True

    def send(self, data):
        if self.assert_unlocked:
            self.assert_unlocked()
        self.sent.append(data)


class BridgePacketTests(TestCase):
    def test_packet_body_eof_raises_disconnect(self):
        handler = make_packet_handler(FakeSocket([b"ab", b""]))
        with self.assertRaises(Disconnect):
            handler.read_sized_packet(4)

    def test_close_is_idempotent(self):
        sock = FakeSocket(fail_shutdown=True)
        handler = make_packet_handler(sock)
        handler.close()
        handler.close()
        self.assertTrue(sock.closed)

    def test_bad_file_descriptor_is_disconnect(self):
        handler = make_packet_handler(BadFdSocket())
        handler.handle()


class JudgeListReliabilityTests(TestCase):
    def test_dispatch_failure_uses_another_compatible_judge(self):
        judges = JudgeList()
        bad = FakeJudge("bad", fail_submit=True, load=0)
        good = FakeJudge("good", load=1)
        judges.judges.add(bad)
        judges.judges.add(good)

        with patch("judge.bridge.judge_list.logger.exception"):
            judges.judge(1, "aplusb", "PY3", "src", None, 4, user_id=1)

        self.assertTrue(bad.disconnected)
        self.assertEqual(good.submitted, [1])
        self.assertIs(judges.submission_map[1], good)

    def test_failed_queued_dispatch_is_requeued(self):
        judges = JudgeList()
        busy = FakeJudge("busy")
        busy.working = True
        bad = FakeJudge("bad", fail_submit=True)
        judges.judges.add(busy)

        judges.judge(1, "aplusb", "PY3", "src", None, 4, user_id=1)
        self.assertIn(1, judges.node_map)

        judges.judges.add(bad)
        with patch("judge.bridge.judge_list.logger.exception"):
            judges._handle_free_judge(bad)

        self.assertTrue(bad.disconnected)
        self.assertIn(1, judges.node_map)
        self.assertNotIn(1, judges.submission_map)

    def test_socket_io_is_outside_lock_for_dispatch_abort_and_broadcast(self):
        judges = JudgeList()

        def assert_unlocked():
            self.assertFalse(judges.lock._is_owned())

        judge = FakeJudge(assert_unlocked=assert_unlocked)
        judges.judges.add(judge)
        judges.judge(1, "aplusb", "PY3", "src", None, 4, user_id=1)
        judges.abort(1)
        judges.broadcast_update_problems()

        self.assertEqual(judge.submitted, [1])
        self.assertIn({"name": "terminate-submission"}, judge.sent)
        self.assertIn({"name": "update-problems"}, judge.sent)

    def test_abort_active_submission_is_not_requeued_on_disconnect(self):
        judges = JudgeList()
        judge = FakeJudge()
        judges.judges.add(judge)
        judges.judge(1, "aplusb", "PY3", "src", None, 4, user_id=1)

        self.assertFalse(judges.abort(1))
        self.assertIn({"name": "terminate-submission"}, judge.sent)
        self.assertNotIn(1, judges.submission_map)

        with patch("judge.bridge.judge_list.logger.warning"):
            sub, working_data = judges.remove(judge)

        self.assertIsNone(sub)
        self.assertEqual(working_data, {})
        self.assertNotIn(1, judges.node_map)

    def test_status_counts_queue_and_active_work(self):
        judges = JudgeList()
        judge = FakeJudge()
        judges.judges.add(judge)
        judges.judge(1, "aplusb", "PY3", "src", None, 4, user_id=1)
        judges.judge(2, "missing", "PY3", "src", None, 4, user_id=2)
        judges.validate("v1", "missing")

        self.assertEqual(
            judges.status(),
            {
                "judges": 1,
                "queued-submissions": 1,
                "active-submissions": 1,
                "queued-validations": 1,
                "active-validations": 0,
            },
        )

    def test_detailed_status_reports_memory_state_without_source(self):
        judges = JudgeList()
        judge = FakeJudge(problems=("aplusb", "other"))
        judges.judges.add(judge)

        judges.judge(1, "aplusb", "PY3", "print(1)", None, 4, user_id=1)
        judges.judge(2, "missing", "PY3", "secret source", "judge", 2, user_id=2)
        status = judges.status(detail=True, include_problems=True)

        self.assertEqual(status["judges"], 1)
        self.assertEqual(status["running-users"], [])
        self.assertEqual(status["judges-detail"][0]["name"], "judge")
        self.assertEqual(status["judges-detail"][0]["problems"], ["aplusb", "other"])
        self.assertEqual(status["active-submissions-detail"][0]["submission-id"], 1)
        self.assertEqual(status["active-submissions-detail"][0]["source-length"], 8)
        self.assertNotIn("source", status["active-submissions-detail"][0])
        self.assertEqual(status["queue"][0]["submission-id"], 2)
        self.assertEqual(status["queue"][0]["judge-id"], "judge")
        self.assertEqual(status["queue"][0]["source-length"], 13)
        self.assertNotIn("source", status["queue"][0])

    def test_stale_submission_completion_does_not_clear_new_owner(self):
        judges = JudgeList()
        old = FakeJudge("old")
        new = FakeJudge("new")
        old._working = 1
        judges.submission_map[1] = new
        judges.submission_users[1] = (99, True)
        judges.running_users.add(99)

        with patch("judge.bridge.judge_list.logger.warning"):
            judges.on_judge_free(old, 1)

        self.assertIs(judges.submission_map[1], new)
        self.assertEqual(judges.submission_users[1], (99, True))
        self.assertIn(99, judges.running_users)

    def test_stale_validation_completion_does_not_clear_new_owner(self):
        judges = JudgeList()
        old = FakeJudge("old")
        new = FakeJudge("new")
        old._working = True
        old._validating = "v1"
        judges.validate_map["v1"] = new

        with patch("judge.bridge.judge_list.logger.warning"):
            judges.on_judge_free_validation(old, "v1")

        self.assertIs(judges.validate_map["v1"], new)

    def test_validation_disconnect_does_not_alias_submission_id_one(self):
        class BoolWorkingJudge(FakeJudge):
            def get_current_submission(self):
                return self._working or None

        judges = JudgeList()
        submission_judge = FakeJudge("submission")
        validation_judge = BoolWorkingJudge("validation")
        validation_judge._working = True
        validation_judge._validating = "v1"
        judges.judges.add(submission_judge)
        judges.judges.add(validation_judge)
        judges.submission_map[1] = submission_judge
        judges.validate_map["v1"] = validation_judge

        with patch("judge.bridge.judge_list.logger.warning"):
            sub, working_data = judges.remove(validation_judge)

        self.assertIsNone(sub)
        self.assertEqual(working_data, {})
        self.assertIs(judges.submission_map[1], submission_judge)
        self.assertNotIn("v1", judges.validate_map)

    def test_duplicate_registration_removes_stale_judge_even_if_disconnect_fails(self):
        judges = JudgeList()
        stale = FakeJudge("judge", fail_disconnect=True)
        replacement = FakeJudge("judge")
        stale._working = 1
        stale._working_data = {
            "problem": "aplusb",
            "language": "PY3",
            "source": "src",
        }
        judges.judges.add(stale)
        judges.submission_map[1] = stale

        with patch("judge.bridge.judge_list.logger.exception"):
            judges.register(replacement)

        self.assertNotIn(stale, judges.judges)
        self.assertIn(replacement, judges.judges)
        self.assertEqual(replacement.submitted, [1])
        self.assertIs(judges.submission_map[1], replacement)

    def test_disconnect_send_failure_requeues_active_submission(self):
        judges = JudgeList()
        bad = FakeJudge("bad", fail_disconnect=True, load=0)
        good = FakeJudge("good", load=1)
        bad._working = 1
        bad._working_data = {
            "problem": "aplusb",
            "language": "PY3",
            "source": "src",
        }
        judges.judges.add(bad)
        judges.judges.add(good)
        judges.submission_map[1] = bad

        with patch("judge.bridge.judge_list.logger.exception"):
            judges.disconnect("bad")

        self.assertNotIn(bad, judges.judges)
        self.assertEqual(good.submitted, [1])
        self.assertIs(judges.submission_map[1], good)


class BridgeStatusTests(TestCase):
    def test_django_handler_bridge_status_packet(self):
        judges = JudgeList()
        judge = FakeJudge()
        judges.judges.add(judge)
        handler = object.__new__(DjangoHandler)
        handler.judges = judges

        self.assertEqual(
            handler.on_bridge_status({}),
            {
                "name": "bridge-status",
                "judges": 1,
                "queued-submissions": 0,
                "active-submissions": 0,
                "queued-validations": 0,
                "active-validations": 0,
            },
        )

    def test_django_handler_bridge_status_detail_packet(self):
        judges = JudgeList()
        judge = FakeJudge(problems=("aplusb", "other"))
        judges.judges.add(judge)
        handler = object.__new__(DjangoHandler)
        handler.judges = judges

        status = handler.on_bridge_status({"detail": True, "include-problems": True})

        self.assertEqual(status["name"], "bridge-status")
        self.assertEqual(status["judges-detail"][0]["name"], "judge")
        self.assertEqual(status["judges-detail"][0]["problems"], ["aplusb", "other"])


class JudgeHandlerDatabaseRetryTests(TestCase):
    def test_retryable_stale_database_error_retries_once(self):
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"
        calls = []

        def packet_handler(data):
            calls.append(data["name"])
            if len(calls) == 1:
                raise OperationalError(2006, "Server has gone away")

        with patch("judge.bridge.judge_handler._ensure_connection"), patch(
            "judge.bridge.judge_handler.db.connection.close"
        ), patch("judge.bridge.judge_handler.logger.warning"):
            handler._handle_packet({"name": "supported-problems"}, packet_handler)

        self.assertEqual(calls, ["supported-problems", "supported-problems"])

    def test_non_retryable_stale_database_error_raises(self):
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"

        def packet_handler(data):
            raise OperationalError(2006, "Server has gone away")

        with patch("judge.bridge.judge_handler._ensure_connection"):
            with self.assertRaises(OperationalError):
                handler._handle_packet({"name": "grading-end"}, packet_handler)


class EmptyProblemTestCaseQuery:
    def order_by(self, *args):
        return self

    def values_list(self, *args, **kwargs):
        return []


class FakeSubmissionForGradingEnd:
    id = 123
    status = "G"
    user_id = 456
    id_secret = "secret"
    contest_object_id = None
    contest_object = None
    judged_date = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    def __init__(self, calls):
        self.calls = calls
        self.problem = SimpleNamespace(
            code="aplusb",
            id=789,
            partial=True,
            points=100,
        )

    def save(self):
        self.calls.append(("save", self.status, self.result))

    def update_contest(self):
        self.calls.append(("update-contest",))


class JudgeHandlerGradingEndTests(TestCase):
    def test_grading_end_saves_verdict_queues_result_json_then_frees_judge(self):
        calls = []
        submission = FakeSubmissionForGradingEnd(calls)
        test_case = SimpleNamespace(
            batch=None,
            memory=64,
            points=1,
            status="AC",
            time=0.1,
            total=1,
        )
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"
        handler.batch_id = None
        handler._submission_result_cases = {
            submission.id: {
                1: {
                    "case": 1,
                    "input": "in",
                    "answer": "ans",
                    "output": "out",
                },
            },
        }
        handler._make_json_log = lambda *args, **kwargs: "{}"
        handler._post_update_submission = lambda *args, **kwargs: calls.append(
            ("post-update",)
        )
        handler._free_self = lambda packet: calls.append(("free",))

        with patch("judge.bridge.judge_handler.Submission") as submission_model, patch(
            "judge.bridge.judge_handler.SubmissionTestCase"
        ) as test_case_model, patch(
            "judge.bridge.judge_handler.ProblemTestCase"
        ) as problem_test_case_model, patch(
            "judge.bridge.judge_handler.save_submission_result_details.delay",
            side_effect=lambda *args: calls.append(("queue-result-json", args)),
        ), patch(
            "judge.bridge.judge_handler.update_user_points.delay",
            side_effect=lambda *args: calls.append(("update-user-points", args)),
        ), patch(
            "judge.bridge.judge_handler.update_problem_stats.delay",
            side_effect=lambda *args: calls.append(("update-problem-stats", args)),
        ), patch(
            "judge.bridge.judge_handler.finished_submission",
            side_effect=lambda *args: calls.append(("finished-submission",)),
        ), patch(
            "judge.bridge.judge_handler.event.post",
            side_effect=lambda *args: calls.append(("event-post", args)),
        ), patch(
            "judge.bridge.judge_handler.json_log.info"
        ):
            submission_model.objects.get.return_value = submission
            test_case_model.objects.filter.return_value = [test_case]
            problem_test_case_model.objects.filter.return_value = (
                EmptyProblemTestCaseQuery()
            )

            handler.on_grading_end({"submission-id": submission.id})

        self.assertEqual(submission.status, "D")
        self.assertNotIn(submission.id, handler._submission_result_cases)
        self.assertLess(calls.index(("save", "D", "AC")), calls.index(("free",)))
        self.assertLess(
            calls.index(("save", "D", "AC")),
            [call[0] for call in calls].index("queue-result-json"),
        )
        queue_call = next(call for call in calls if call[0] == "queue-result-json")
        self.assertEqual(queue_call[1][0], submission.id)
        self.assertEqual(queue_call[1][1], submission.judged_date.isoformat())
        self.assertEqual(calls[-1], ("free",))

    def test_grading_end_still_frees_judge_when_result_json_enqueue_fails(self):
        calls = []
        submission = FakeSubmissionForGradingEnd(calls)
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"
        handler.batch_id = None
        handler._submission_result_cases = {submission.id: {}}
        handler._make_json_log = lambda *args, **kwargs: "{}"
        handler._post_update_submission = lambda *args, **kwargs: calls.append(
            ("post-update",)
        )
        handler._free_self = lambda packet: calls.append(("free",))

        with patch("judge.bridge.judge_handler.Submission") as submission_model, patch(
            "judge.bridge.judge_handler.SubmissionTestCase"
        ) as test_case_model, patch(
            "judge.bridge.judge_handler.ProblemTestCase"
        ) as problem_test_case_model, patch(
            "judge.bridge.judge_handler.save_submission_result_details.delay",
            side_effect=RuntimeError("broker unavailable"),
        ), patch(
            "judge.bridge.judge_handler.update_user_points.delay"
        ), patch(
            "judge.bridge.judge_handler.update_problem_stats.delay"
        ), patch(
            "judge.bridge.judge_handler.finished_submission"
        ), patch(
            "judge.bridge.judge_handler.event.post"
        ), patch(
            "judge.bridge.judge_handler.json_log.info"
        ), patch(
            "judge.bridge.judge_handler.logger.exception"
        ):
            submission_model.objects.get.return_value = submission
            test_case_model.objects.filter.return_value = []
            problem_test_case_model.objects.filter.return_value = (
                EmptyProblemTestCaseQuery()
            )

            handler.on_grading_end({"submission-id": submission.id})

        self.assertIn(("save", "D", "SC"), calls)
        self.assertEqual(calls[-1], ("free",))

    def test_grading_end_frees_judge_for_aborted_submission(self):
        calls = []
        submission = FakeSubmissionForGradingEnd(calls)
        submission.status = "AB"
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"
        handler.batch_id = None
        handler._submission_result_cases = {submission.id: {}}
        handler._make_json_log = lambda *args, **kwargs: "{}"
        handler._free_self = lambda packet: calls.append(("free",))

        with patch("judge.bridge.judge_handler.Submission") as submission_model, patch(
            "judge.bridge.judge_handler.json_log.info"
        ):
            submission_model.objects.get.return_value = submission

            handler.on_grading_end({"submission-id": submission.id})

        self.assertNotIn(submission.id, handler._submission_result_cases)
        self.assertEqual(calls, [("free",)])

    def test_grading_end_frees_judge_when_later_side_effect_raises(self):
        calls = []
        submission = FakeSubmissionForGradingEnd(calls)
        handler = object.__new__(JudgeHandler)
        handler.name = "judge"
        handler.batch_id = None
        handler._submission_result_cases = {submission.id: {}}
        handler._make_json_log = lambda *args, **kwargs: "{}"
        handler._free_self = lambda packet: calls.append(("free",))

        with patch("judge.bridge.judge_handler.Submission") as submission_model, patch(
            "judge.bridge.judge_handler.SubmissionTestCase"
        ) as test_case_model, patch(
            "judge.bridge.judge_handler.ProblemTestCase"
        ) as problem_test_case_model, patch(
            "judge.bridge.judge_handler.save_submission_result_details.delay"
        ), patch(
            "judge.bridge.judge_handler.update_user_points.delay",
            side_effect=RuntimeError("broker unavailable"),
        ), patch(
            "judge.bridge.judge_handler.json_log.info"
        ):
            submission_model.objects.get.return_value = submission
            test_case_model.objects.filter.return_value = []
            problem_test_case_model.objects.filter.return_value = (
                EmptyProblemTestCaseQuery()
            )

            with self.assertRaises(RuntimeError):
                handler.on_grading_end({"submission-id": submission.id})

        self.assertIn(("save", "D", "SC"), calls)
        self.assertEqual(calls[-1], ("free",))


class SubmissionResultDetailsTaskTests(TestCase):
    judged_date = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    def test_current_judging_generation_writes_result(self):
        submission = SimpleNamespace(status="D", judged_date=self.judged_date)
        cases = [{"case": 1, "output": "out"}]

        with patch(
            "judge.tasks.submission.Submission.objects.select_for_update"
        ) as select_for_update, patch(
            "judge.tasks.submission.save_submission_result", return_value="result.json"
        ) as save_result, patch(
            "judge.tasks.submission.transaction.atomic", return_value=nullcontext()
        ):
            select_for_update.return_value.only.return_value.get.return_value = (
                submission
            )

            result = save_submission_result_details.run(
                123, self.judged_date.isoformat(), cases
            )

        self.assertEqual(result, "result.json")
        save_result.assert_called_once_with(123, cases)

    def test_stale_judging_generation_does_not_write_result(self):
        submission = SimpleNamespace(
            status="D",
            judged_date=datetime(2026, 8, 23, 12, 1, tzinfo=timezone.utc),
        )

        with patch(
            "judge.tasks.submission.Submission.objects.select_for_update"
        ) as select_for_update, patch(
            "judge.tasks.submission.save_submission_result"
        ) as save_result, patch(
            "judge.tasks.submission.transaction.atomic", return_value=nullcontext()
        ):
            select_for_update.return_value.only.return_value.get.return_value = (
                submission
            )

            result = save_submission_result_details.run(
                123,
                self.judged_date.isoformat(),
                [{"case": 1, "output": "old"}],
            )

        self.assertIsNone(result)
        save_result.assert_not_called()

    def test_queued_rejudge_does_not_write_previous_result(self):
        submission = SimpleNamespace(status="QU", judged_date=self.judged_date)

        with patch(
            "judge.tasks.submission.Submission.objects.select_for_update"
        ) as select_for_update, patch(
            "judge.tasks.submission.save_submission_result"
        ) as save_result, patch(
            "judge.tasks.submission.transaction.atomic", return_value=nullcontext()
        ):
            select_for_update.return_value.only.return_value.get.return_value = (
                submission
            )

            result = save_submission_result_details.run(
                123,
                self.judged_date.isoformat(),
                [{"case": 1, "output": "old"}],
            )

        self.assertIsNone(result)
        save_result.assert_not_called()


class PacketEncodingTests(TestCase):
    def test_packet_handler_decompresses_valid_payload(self):
        payload = zlib.compress(json.dumps({"name": "ping"}).encode("utf-8"))
        handler = make_packet_handler(FakeSocket([payload]))
        handler.read_sized_packet(len(payload))
        self.assertEqual(handler.packet, '{"name": "ping"}')
