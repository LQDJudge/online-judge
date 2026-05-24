import time
from dataclasses import dataclass, field

from django.db import transaction

from judge.models import Problem, Submission, SubmissionSource


class ProblemEquivalenceError(Exception):
    pass


@dataclass
class ProblemEquivalenceVerifier:
    source_code: str
    target_code: str
    source_submission_id: int | None = None
    apply: bool = False
    judge_id: str | None = None
    wait_seconds: int = 0
    poll_interval: float = 2.0
    report: dict = field(default_factory=dict)

    def run(self):
        with transaction.atomic():
            self.source = Problem.objects.get(code=self.source_code)
            self.target = Problem.objects.get(code=self.target_code)
            if self.source.id == self.target.id:
                raise ProblemEquivalenceError(
                    "source and target must be different problems"
                )

            source_submission = self._get_source_submission()
            self.report = self._build_report(source_submission)
            if not self.apply:
                return self.report

            verification_submission = self._clone_submission(source_submission)
            self.report["verification_submission_id"] = verification_submission.id

        queued = verification_submission.judge(rejudge=False, judge_id=self.judge_id)
        self.report["queued"] = queued
        if self.wait_seconds:
            self._wait_for_result(verification_submission)
        return self.report

    def _get_source_submission(self):
        queryset = (
            Submission.objects.select_related("problem", "language", "user", "source")
            .filter(
                problem=self.source,
                status="D",
                result="AC",
                source__isnull=False,
            )
            .order_by("-case_points", "-points", "-date", "-id")
        )
        if self.source_submission_id:
            try:
                submission = queryset.get(id=self.source_submission_id)
            except Submission.DoesNotExist as exc:
                raise ProblemEquivalenceError(
                    "source submission must be an accepted submission for the source problem"
                ) from exc
            return submission

        submission = queryset.first()
        if submission is None:
            raise ProblemEquivalenceError(
                "no accepted source submission with stored source code was found"
            )
        return submission

    def _build_report(self, source_submission):
        return {
            "applied": self.apply,
            "source": {
                "id": self.source.id,
                "code": self.source.code,
                "name": self.source.name,
            },
            "target": {
                "id": self.target.id,
                "code": self.target.code,
                "name": self.target.name,
            },
            "source_submission_id": source_submission.id,
            "source_submission_result": source_submission.result,
            "source_submission_points": source_submission.points,
            "language": source_submission.language.key,
            "queued": False,
            "verification_submission_id": None,
            "verification_status": None,
            "verification_result": None,
            "passed": None,
        }

    def _clone_submission(self, source_submission):
        verification_submission = Submission.objects.create(
            user=source_submission.user,
            problem=self.target,
            language=source_submission.language,
            status="QU",
            result=None,
            points=None,
            case_points=0,
            case_total=0,
            time=None,
            memory=None,
        )
        SubmissionSource.objects.create(
            submission=verification_submission,
            source=source_submission.source.source,
        )
        return verification_submission

    def _wait_for_result(self, verification_submission):
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() <= deadline:
            verification_submission.refresh_from_db()
            if verification_submission.is_graded:
                break
            time.sleep(self.poll_interval)

        verification_submission.refresh_from_db()
        self.report["verification_status"] = verification_submission.status
        self.report["verification_result"] = verification_submission.result
        self.report["verification_points"] = verification_submission.points
        self.report["verification_case_points"] = verification_submission.case_points
        self.report["verification_case_total"] = verification_submission.case_total
        self.report["passed"] = (
            verification_submission.status == "D"
            and verification_submission.result == "AC"
            and (
                not verification_submission.case_total
                or verification_submission.case_points
                == verification_submission.case_total
            )
        )
