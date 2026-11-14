"""Short database claims and direct dependency scheduling on the default queue."""

import logging
import uuid
from functools import partial

from celery import current_app
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import ContestReviewRun, ProblemData, ProblemReviewRun, ReviewStage
from judge.auto_review.phases import (
    stage_definitions,
    stage_dependencies_ready,
    effective_readiness,
)

logger = logging.getLogger(__name__)
ACTIVE = ("R", "B")


def run_model(kind):
    if kind not in ("problem", "contest"):
        raise ValueError("Invalid review kind")
    return ProblemReviewRun if kind == "problem" else ContestReviewRun


def definitions_for_run(run):
    kind = "problem" if isinstance(run, ProblemReviewRun) else "contest"
    data = (
        (
            ProblemData.objects.filter(problem_id=run.problem_id)
            .values("zipfile", "output_only", "testcase_validator")
            .first()
            or {}
        )
        if kind == "problem"
        else {}
    )
    context = {
        "artifacts": {
            "zipfile": data.get("zipfile"),
            "testcase_validator": data.get("testcase_validator"),
        },
        "judging_profile": {
            "submission": "output" if data.get("output_only") else "source"
        },
    }
    return stage_definitions(kind, context)


def settle(run, *, cancelled=False, preflight=False):
    if run.credit_settled:
        return
    unresolved = (
        run.coordinator_stages.exclude(error_category="")
        .exclude(status=ReviewStage.DONE)
        .exclude(stage_id="extract_stats")
        .exists()
    )
    if isinstance(run, ContestReviewRun):
        unresolved |= (
            ReviewStage.objects.filter(
                problem_run__parent_dependencies__contest_run=run,
            )
            .exclude(error_category="")
            .exclude(status=ReviewStage.DONE)
            .exclude(stage_id="extract_stats")
            .exists()
        )
    unresolved |= run.status == run.BLOCKED
    run.credit_consumed = not (run.trigger_is_superuser or preflight or unresolved)
    run.credit_settled = True
    run.save(update_fields=["credit_consumed", "credit_settled"])


def _dispatch(kind, run_id, key, token, countdown):
    claim = ReviewStage.objects.filter(
        **{kind + "_run_id": run_id, kind + "_run__status__in": ACTIVE},
        stage_id=key,
        status=ReviewStage.QUEUED,
        lease_token=token,
    )
    for attempt in range(3):
        if not claim.exists():
            return
        try:
            current_app.send_task(
                "judge.tasks.coordinator.run_review_stage",
                args=[kind, run_id, key, str(token)],
                countdown=countdown,
                retry=False,
            )
            return
        except Exception:
            logger.exception(
                "Review task delivery failed kind=%s run=%s phase=%s attempt=%s",
                kind,
                run_id,
                key,
                attempt + 1,
            )
    if key == "extract_stats":
        changes = {
            "status": ReviewStage.DONE,
            "output_json": {"availability": "unavailable"},
            "finished_at": timezone.now(),
            "lease_token": None,
        }
    else:
        changes = {
            "status": ReviewStage.ERROR,
            "error_category": "broker_delivery",
            "reason": _("The review task could not be delivered. Retry this section."),
            "lease_token": None,
        }
    if claim.update(**changes):
        schedule_phases(kind, run_id, key)


def _queue_recovery(kind, run_id):
    try:
        current_app.send_task(
            "judge.tasks.coordinator.emit_recovery", args=[kind, run_id], retry=False
        )
    except Exception:
        logger.exception(
            "Could not queue review recovery notification kind=%s run=%s", kind, run_id
        )


def notify_parents(run):
    if isinstance(run, ProblemReviewRun):
        parents = list(
            run.parent_dependencies.filter(contest_run__status__in=ACTIVE).values_list(
                "contest_run_id", flat=True
            )
        )
        for parent in parents:
            transaction.on_commit(partial(schedule_phases, "contest", parent))
        if run.status == run.DONE:
            transaction.on_commit(
                lambda: current_app.send_task(
                    "judge.tasks.coordinator.refresh_review_freshness",
                    args=[run.problem_id],
                )
            )


def schedule_phases(kind, run_id, completed_phase=None):
    model = run_model(kind)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        if run.status not in ACTIVE:
            return
        stages = {stage.stage_id: stage for stage in run.coordinator_stages.all()}
        definitions = definitions_for_run(run)
        if set(stages) != set(definitions):
            logger.error("Review phase graph mismatch kind=%s run=%s", kind, run_id)
            run.status, run.readiness = run.BLOCKED, "INCOMPLETE"
            run.save(update_fields=["status", "readiness"])
            return
        if kind == "contest":
            children = list(run.child_dependencies.select_related("problem_run"))
            if any(
                dep.problem_run and dep.problem_run.status == "C" for dep in children
            ):
                run.status, run.finished_at = run.CANCELLED, timezone.now()
                settle(run, cancelled=True)
                run.save(update_fields=["status", "finished_at"])
                return
            if any(
                dep.problem_run and dep.problem_run.status in ACTIVE for dep in children
            ):
                blocked = any(
                    dep.problem_run and dep.problem_run.status == "B"
                    for dep in children
                )
                run.status = run.BLOCKED if blocked else run.RUNNING
                run.save(update_fields=["status"])
                return
        if all(stage.status == ReviewStage.DONE for stage in stages.values()):
            run.readiness = effective_readiness(run)
            run.summary_report = stages["final_assessment"].output_json.get(
                "feedback", ""
            )
            run.status, run.finished_at = run.DONE, timezone.now()
            run.save(
                update_fields=["readiness", "summary_report", "status", "finished_at"]
            )
            settle(run)
            notify_parents(run)
            transaction.on_commit(
                lambda: current_app.send_task(
                    "judge.tasks.coordinator.emit_completion", args=[kind, run_id]
                )
            )
            return
        if completed_phase and completed_phase.startswith("__retry__"):
            candidates = [completed_phase[len("__retry__") :]]
        else:
            candidates = [
                key
                for key, definition in definitions.items()
                if (
                    completed_phase in definition["dependencies"]
                    if completed_phase
                    else not definition["dependencies"]
                )
            ]
        for key in candidates:
            stage = stages.get(key)
            if (
                not stage
                or stage.status != ReviewStage.PENDING
                or not stage_dependencies_ready(definitions[key], stages)
            ):
                continue
            stage.status, stage.lease_token = ReviewStage.QUEUED, uuid.uuid4()
            stage.save(update_fields=["status", "lease_token"])
            delay = (
                max(0, (stage.retry_at - timezone.now()).total_seconds())
                if stage.retry_at
                else 0
            )
            transaction.on_commit(
                partial(_dispatch, kind, run_id, key, stage.lease_token, delay)
            )
        statuses = list(run.coordinator_stages.values_list("status", flat=True))
        run.status = (
            run.BLOCKED
            if ReviewStage.ERROR in statuses
            and not any(
                s in (ReviewStage.QUEUED, ReviewStage.RUNNING) for s in statuses
            )
            else run.RUNNING
        )
        run.save(update_fields=["status"])
        if run.status == run.BLOCKED:
            notify_parents(run)
            transaction.on_commit(partial(_queue_recovery, kind, run_id))
