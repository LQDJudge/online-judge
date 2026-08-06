import errno
import json
import zlib
from unittest import TestCase
from unittest.mock import patch

from django.db import OperationalError

from judge.bridge.base_handler import Disconnect, ZlibPacketHandler
from judge.bridge.django_handler import DjangoHandler
from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import JudgeList


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


class PacketEncodingTests(TestCase):
    def test_packet_handler_decompresses_valid_payload(self):
        payload = zlib.compress(json.dumps({"name": "ping"}).encode("utf-8"))
        handler = make_packet_handler(FakeSocket([payload]))
        handler.read_sized_packet(len(payload))
        self.assertEqual(handler.packet, '{"name": "ping"}')
