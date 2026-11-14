import json
import logging
import struct

from django import db

from judge.bridge.base_handler import Disconnect, ZlibPacketHandler
from judge.auto_review.test_statistics import (
    active_extraction,
    parse_extraction_request_id,
)

logger = logging.getLogger("judge.bridge")
size_pack = struct.Struct("!I")


class DjangoHandler(ZlibPacketHandler):
    def __init__(self, request, client_address, server, judges):
        super().__init__(request, client_address, server)

        self.handlers = {
            "submission-request": self.on_submission,
            "terminate-submission": self.on_termination,
            "disconnect-judge": self.on_disconnect_request,
            "validate-request": self.on_validate_request,
            "test-statistics-request": self.on_test_statistics_request,
            "cancel-test-statistics": self.on_cancel_test_statistics,
            "update-problems": self.on_update_problems,
            "bridge-status": self.on_bridge_status,
        }
        self.judges = judges

    def send(self, data):
        super().send(json.dumps(data, separators=(",", ":")))

    def on_packet(self, packet):
        packet = json.loads(packet)
        try:
            result = self.handlers.get(packet.get("name", None), self.on_malformed)(
                packet
            )
        except Exception:
            logger.exception("Error in packet handling (Django-facing)")
            result = {"name": "bad-request"}
        self.send(result)
        raise Disconnect()

    def on_submission(self, data):
        id = data["submission-id"]
        problem = data["problem-id"]
        language = data["language"]
        source = data["source"]
        judge_id = data["judge-id"]
        priority = data["priority"]
        user_id = data.get("user-id")
        if not self.judges.check_priority(priority):
            return {"name": "bad-request"}
        self.judges.judge(id, problem, language, source, judge_id, priority, user_id)
        return {"name": "submission-received", "submission-id": id}

    def on_termination(self, data):
        return {
            "name": "submission-received",
            "judge-aborted": self.judges.abort(data["submission-id"]),
        }

    def on_validate_request(self, data):
        validate_id = data["validate-id"]
        problem_id = data["problem-id"]
        success = self.judges.validate(validate_id, problem_id)
        return {
            "name": "validate-received" if success else "validate-failed",
            "validate-id": validate_id,
        }

    def on_update_problems(self, data):
        self.judges.broadcast_update_problems()
        return {"name": "update-problems-received"}

    def on_test_statistics_request(self, data):
        identity = data.get("request-id")
        stage_id, token = parse_extraction_request_id(identity)
        stage = active_extraction(stage_id, token)
        request = data.get("request")
        if stage is None or not isinstance(request, dict):
            return {"name": "test-statistics-failed", "request-id": identity}
        if (
            set(request)
            != {"problem_code", "phase_id", "attempt_token", "helper_source"}
            or request["problem_code"] != stage.problem_run.problem.code
            or request["phase_id"] != stage_id
            or isinstance(request["phase_id"], bool)
            or request["attempt_token"] != str(token)
            or not isinstance(request["helper_source"], str)
            or not 0 < len(request["helper_source"].encode("utf8")) <= 256 * 1024
        ):
            return {"name": "test-statistics-failed", "request-id": identity}
        success = self.judges.validate(
            identity, request["problem_code"], review=True, protocol=1, request=request
        )
        return {
            "name": "test-statistics-received" if success else "test-statistics-failed",
            "request-id": identity,
        }

    def on_cancel_test_statistics(self, data):
        identities = data.get("request-ids")
        if not isinstance(identities, list) or len(identities) > 128:
            return {"name": "cancel-test-statistics-failed"}
        for identity in identities:
            parse_extraction_request_id(identity)
        self.judges.cancel_statistics(identities)
        return {"name": "cancel-test-statistics-received"}

    def on_bridge_status(self, data):
        return {
            "name": "bridge-status",
            **self.judges.status(
                detail=bool(data.get("detail")),
                include_problems=bool(data.get("include-problems")),
            ),
        }

    def on_disconnect_request(self, data):
        judge_id = data["judge-id"]
        force = data["force"]
        self.judges.disconnect(judge_id, force=force)
        return {"name": "disconnect-judge-received"}

    def on_malformed(self, packet):
        logger.error("Malformed packet: %s", packet)

    def on_cleanup(self):
        db.connection.close()
