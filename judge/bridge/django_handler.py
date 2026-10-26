import json
import logging
import struct

from django import db

from judge.bridge.base_handler import Disconnect, ZlibPacketHandler
from judge.models.review_evidence import ReviewEvidenceJob
from judge.review.protocol import required_evidence_protocol

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
            "review-evidence-request": self.on_review_evidence_request,
            "cancel-review-evidence": self.on_cancel_review_evidence,
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

    def on_review_evidence_request(self, data):
        job = ReviewEvidenceJob.objects.select_related("problem_run__problem").get(
            job_id=data["job-id"], status="P"
        )
        success = self.judges.validate(
            str(job.job_id),
            job.problem_run.problem.code,
            review=True,
            protocol=required_evidence_protocol(job.operation, job.payload),
        )
        return {
            "name": "review-evidence-received" if success else "review-evidence-failed",
            "job-id": str(job.job_id),
        }

    def on_cancel_review_evidence(self, data):
        identities = data.get("job-ids", [])
        if not isinstance(identities, list) or len(identities) > 128:
            return {"name": "cancel-review-evidence-failed"}
        for identity in ReviewEvidenceJob.objects.filter(
            job_id__in=identities, status="C"
        ).values_list("job_id", flat=True):
            key = str(identity)
            handler = self.judges.validate_map.get(key)
            if handler is not None and handler._validating == key:
                handler.send({"name": "terminate-review-evidence", "validate-id": key})
        return {"name": "cancel-review-evidence-received"}

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
