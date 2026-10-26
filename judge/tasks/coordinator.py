"""Persisted DAG execution, safe redelivery and section-only recovery."""

import json
import logging
import math
import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from celery import shared_task

from judge.models import (
    ContestReviewRun,
    Problem,
    ProblemReviewRun,
    ReviewStage,
    ReviewStageAttempt,
)
from judge.models.notification import Notification, NotificationCategory
from judge.review.coordinator import (
    CoordinatorInfrastructureError,
    ReviewExecutionPending,
    build_stage_input,
    effective_readiness,
    execute_stage,
    stage_dependencies_ready,
    stage_definitions,
)
from judge.review.workflow import (
    ACTIVE,
    enqueue,
    invalidate_review_sections,
    invalidate_readiness,
    settle,
)
from judge.review.dispatch import dispatch_stage
from judge.review.presentation import review_author_feedback
from judge.review.snapshots import SnapshotError

logger = logging.getLogger(__name__)


def run_model(kind):
    if kind not in ("problem", "contest"):
        raise ValueError("Invalid review kind")
    return ProblemReviewRun if kind == "problem" else ContestReviewRun


def _send_stage(kind, run_id, stage_id, countdown=0):
    transaction.on_commit(lambda: dispatch_stage(kind, run_id, stage_id, countdown))


def _notify_parents(run):
    if isinstance(run, ProblemReviewRun):
        if run.status == run.DONE:
            transaction.on_commit(
                lambda: refresh_review_freshness.delay(run.problem_id)
            )
        for parent_id in run.parent_dependencies.values_list(
            "contest_run_id", flat=True
        ):
            enqueue("contest", parent_id)


@shared_task
def refresh_review_freshness(problem_id):
    """Update indicators after completed evidence; never start an AI review."""
    problem = Problem.objects.filter(pk=problem_id).first()
    if problem:
        invalidate_readiness(problem)


def failure_category(stage_id, error):
    category = getattr(error, "error_category", "")
    if category and category != "provider":
        return category
    if isinstance(error, DatabaseError):
        return "database"
    if isinstance(error, (SnapshotError, OSError)):
        return "artifact_access"
    if stage_id == "S":
        return "semantic_retrieval"
    if stage_id in (
        "P_EXTRACT",
        "P_EXTRACT_FINAL",
        "P_VALIDATOR",
        "P_CHECKER",
        "P_FOLLOWUP",
    ):
        return "judge_execution"
    return category or "infrastructure"


@shared_task
def advance_review(kind, run_id):
    model = run_model(kind)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        if run.workflow_version != 2 or run.status not in ACTIVE:
            return
        definitions = stage_definitions(kind)
        fk = {kind + "_run": run}
        existing = set(run.coordinator_stages.values_list("stage_id", flat=True))
        ReviewStage.objects.bulk_create(
            [
                ReviewStage(**fk, stage_id=stage_id)
                for stage_id in definitions
                if stage_id not in existing
            ]
        )
        stages = {stage.stage_id: stage for stage in run.coordinator_stages.all()}
        if kind == "contest":
            children = list(run.child_dependencies.select_related("problem_run"))
            if any(
                dep.problem_run and dep.problem_run.status == "R" for dep in children
            ):
                run.status = run.RUNNING
                run.readiness = "INCOMPLETE"
                run.save(update_fields=["status", "readiness"])
                enqueue(kind, run.pk, countdown=15)
                return
            child_blocked = any(
                dep.problem_run and dep.problem_run.status == "B" for dep in children
            )
            dossier = stages.get("E")
            if dossier and dossier.status == ReviewStage.DONE:
                current_children = build_stage_input(run, "E").get("children", [])
                if dossier.input_json.get("children") != current_children:
                    invalidate_review_sections(run, {"E"}, definitions)
                    stages = {
                        stage.stage_id: stage for stage in run.coordinator_stages.all()
                    }
        else:
            child_blocked = False
        if all(stage.status == ReviewStage.DONE for stage in stages.values()):
            run.readiness = effective_readiness(run)
            run.status = run.BLOCKED if child_blocked else run.DONE
            run.finished_at = None if child_blocked else timezone.now()
            final = stages.get("R_FINAL") or stages.get("R")
            run.summary_report = (
                review_author_feedback(final.output_json) if final else ""
            )
            run.save(
                update_fields=["status", "readiness", "finished_at", "summary_report"]
            )
            if not child_blocked:
                settle(run)
            _notify_parents(run)
            if not child_blocked:
                transaction.on_commit(lambda: emit_completion(kind, run_id))
            return
        schedulable = []
        for stage_id, definition in definitions.items():
            stage = stages[stage_id]
            if stage.status != ReviewStage.PENDING:
                continue
            if stage_dependencies_ready(definition, stages):
                delay = (
                    max(0, (stage.retry_at - timezone.now()).total_seconds())
                    if stage.retry_at
                    else 0
                )
                schedulable.append((stage_id, delay))
        for stage_id, delay in schedulable:
            _send_stage(kind, run_id, stage_id, countdown=delay)
        missing = child_blocked or any(
            stage.status == ReviewStage.ERROR for stage in stages.values()
        )
        final = stages.get("R_FINAL") or stages.get("R")
        if final and final.status == ReviewStage.DONE:
            run.summary_report = review_author_feedback(final.output_json)
        run.status = run.RUNNING
        run.readiness = "INCOMPLETE" if missing else ""
        if not schedulable and not any(
            stage.status == ReviewStage.RUNNING for stage in stages.values()
        ):
            run.status = run.BLOCKED
            run.readiness = "INCOMPLETE"
            _notify_parents(run)
        run.save(update_fields=["status", "readiness", "summary_report"])


@shared_task
def run_review_stage(kind, run_id, stage_id):
    model = run_model(kind)
    definitions = stage_definitions(kind)
    if stage_id not in definitions:
        raise ValueError("Unknown review stage")
    token = uuid.uuid4()
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        if run.status not in ACTIVE or run.workflow_version != 2:
            return
        stage = run.coordinator_stages.select_for_update().get(stage_id=stage_id)
        if stage.status != ReviewStage.PENDING or (
            stage.retry_at and stage.retry_at > timezone.now()
        ):
            return
        stages = {item.stage_id: item for item in run.coordinator_stages.all()}
        if not stage_dependencies_ready(definitions[stage_id], stages):
            return
        stage.status = ReviewStage.RUNNING
        stage.lease_token = token
        stage.lease_expires_at = timezone.now() + timedelta(
            seconds=getattr(settings, "REVIEW_STAGE_LEASE_SECONDS", 600)
        )
        stage.started_at = stage.started_at or timezone.now()
        stage.attempts += 1
        stage.retry_at = None
        stage.save()
        attempt = ReviewStageAttempt.objects.create(
            stage=stage,
            number=stage.attempts,
            diagnostics_expires_at=timezone.now()
            + timedelta(days=getattr(settings, "REVIEW_DIAGNOSTIC_RETENTION_DAYS", 30)),
        )
    output = None
    run._review_stage_claim = (stage.pk, token)
    pending = False
    error = None
    raw_response = ""
    try:
        if not stage.input_json:
            context = build_stage_input(run, stage_id)
            # CAS: cancelled/replaced leases cannot write even retry context.
            if not ReviewStage.objects.filter(pk=stage.pk, lease_token=token).update(
                input_json=context
            ):
                return
            stage.input_json = context
        attempt.raw_request = json.dumps(stage.input_json, ensure_ascii=False)
        attempt.save(update_fields=["raw_request"])
        output = execute_stage(run, stage_id, stage.input_json)
        if not isinstance(output, dict):
            raise CoordinatorInfrastructureError("invalid_response")
        raw_response = output.pop("_debug_raw_response", "")
    except ReviewExecutionPending:
        pending = True
    except Exception as exc:
        error = exc
        logger.exception("Coordinator stage %s failed (%s #%s)", stage_id, kind, run_id)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        stage = ReviewStage.objects.select_for_update().get(pk=stage.pk)
        # Preserve incurred attempt metadata even if cancellation won the race.
        attempt.finished_at = timezone.now()
        attempt.raw_response = (
            raw_response
            or getattr(error, "raw_response", "")
            or (json.dumps(output, ensure_ascii=False) if output is not None else "")
        )
        attempt.error = (
            "%s: %s" % (type(error).__name__, str(error)[:2000]) if error else ""
        )
        attempt.usage_json = (
            output.get("_usage", output.get("_request", {}))
            if output
            else getattr(error, "usage_json", {})
        )
        attempt.save(
            update_fields=[
                "finished_at",
                "raw_response",
                "error",
                "usage_json",
            ]
        )
        if run.status not in ACTIVE or stage.lease_token != token:
            return
        stage.lease_token = None
        stage.lease_expires_at = None
        if pending:
            # Waiting for an already scheduled judge job is not another provider
            # attempt or infrastructure failure. Keep durable job state in P.
            stage.attempts -= 1
            attempt.delete()
            stage.status = ReviewStage.PENDING
            stage.retry_at = timezone.now() + timedelta(seconds=15)
        elif error:
            stage.error_category = failure_category(stage_id, error)
            stage.reason = (
                str(error)
                if stage.error_category.startswith(("timeout_", "provider_"))
                else _("This section encountered an infrastructure issue.")
            )
            auto_retry = (
                stage.attempts < 3
                and stage.manual_retries == 0
                and stage.error_category not in ("configuration", "context_limit")
                and getattr(error, "retryable", True)
            )
            retry_after = getattr(error, "retry_after", None)
            if retry_after is not None and (
                not isinstance(retry_after, (int, float))
                or not math.isfinite(retry_after)
                or retry_after
                > getattr(settings, "REVIEW_MAX_PROVIDER_RETRY_DELAY", 3600)
            ):
                # Do not ignore a long provider cooldown and hammer the API.
                auto_retry = False
            stage.status = ReviewStage.PENDING if auto_retry else ReviewStage.ERROR
            if auto_retry:
                stage.retry_at = timezone.now() + timedelta(
                    seconds=max(15 * 2 ** (stage.attempts - 1), retry_after or 0)
                    + random.uniform(0, 3)
                )
            elif (
                isinstance(retry_after, (int, float))
                and math.isfinite(retry_after)
                and retry_after > 0
            ):
                # Also protect a manual retry after automatic attempts exhaust.
                stage.retry_at = timezone.now() + timedelta(
                    seconds=min(retry_after, 86400)
                )
        else:
            stage.status = ReviewStage.DONE
            stage.output_json = output
            if stage.input_json.get("missing_evidence"):
                stage.output_json["_missing_evidence"] = stage.input_json[
                    "missing_evidence"
                ]
                if stage.output_json.get("status") == "no_issue":
                    stage.output_json["status"] = "uncertain"
                if stage_id in ("R", "R_FINAL"):
                    stage.output_json["readiness"] = "INCOMPLETE"
            stage.error_category = ""
            stage.reason = ""
            stage.finished_at = timezone.now()
        stage.save()
        if stage_id == "D" and output and output.get("errors"):
            run.status = run.DONE
            run.readiness = "INCOMPLETE"
            run.finished_at = timezone.now()
            run.summary_report = "\n".join(str(value) for value in output["errors"])
            run.save(
                update_fields=["status", "readiness", "finished_at", "summary_report"]
            )
            settle(run, preflight=True)
            _notify_parents(run)
            transaction.on_commit(lambda: emit_completion(kind, run_id))
            return
        enqueue(kind, run_id)


@shared_task
def emit_completion(kind, run_id):
    # Commit the notification and receipt together: a transient notification
    # failure must neither lose the alert nor re-spend a completed review.
    model = run_model(kind)
    with transaction.atomic():
        run = (
            model.objects.select_for_update()
            .select_related("triggered_by")
            .get(pk=run_id)
        )
        if run.notifications_sent or run.status != model.DONE:
            return
        obj = run.problem if kind == "problem" else run.contest
        emit_review_notification(kind, obj, run.pk, readiness=run.readiness)
        run.notifications_sent = True
        run.save(update_fields=["notifications_sent"])


def emit_review_notification(kind, obj, run_id, *, readiness=""):
    category = (
        NotificationCategory.PUBLIC_REQUEST_REVIEW_DONE
        if kind == "problem"
        else NotificationCategory.CONTEST_PUBLIC_REQUEST_REVIEW_DONE
    )
    url = reverse(
        kind + "_review_dashboard", args=[obj.code if kind == "problem" else obj.key]
    )
    Notification.objects.bulk_create_notifications(
        user_ids=list(obj.authors.values_list("pk", flat=True)),
        category=category,
        html_link=format_html('<a href="{}?run_id={}">{}</a>', url, run_id, obj.name),
        author=None,
        extra_data={"run_id": run_id, "readiness": readiness},
        deduplicate=False,
    )


@shared_task
def cleanup_review_diagnostics():
    now = timezone.now()
    return ReviewStageAttempt.objects.filter(diagnostics_expires_at__lte=now).filter(
        retained_until__isnull=True,
    ).update(
        raw_request="", raw_response="", error=""
    ) + ReviewStageAttempt.objects.filter(
        diagnostics_expires_at__lte=now,
        retained_until__lte=now,
    ).update(
        raw_request="", raw_response="", error=""
    )


@shared_task
def recover_review_leases():
    now = timezone.now()
    deliveries = ReviewStage.objects.filter(
        status=ReviewStage.PENDING, error_category="broker_delivery", retry_at__lte=now
    ).select_related("problem_run", "contest_run")
    for stage in deliveries[:100]:
        if stage.run.status in ACTIVE:
            dispatch_stage(stage.kind, stage.run.pk, stage.stage_id)
    # Lost callbacks/messages with no lease or terminal delivery error are
    # recovered on the same run. No provider attempt is reset or rerun here.
    for kind, model in (("problem", ProblemReviewRun), ("contest", ContestReviewRun)):
        lost = (
            model.objects.filter(workflow_version=2, status__in=ACTIVE)
            .exclude(coordinator_stages__status=ReviewStage.RUNNING)
            .exclude(coordinator_stages__error_category="broker_delivery")
            .exclude(coordinator_stages__status=ReviewStage.ERROR)
            .values_list("pk", flat=True)
            .distinct()[:100]
        )
        for run_id in lost:
            enqueue(kind, run_id)
    for stage in ReviewStage.objects.filter(
        status=ReviewStage.RUNNING, lease_expires_at__lte=now
    ).iterator():
        with transaction.atomic():
            locked = ReviewStage.objects.select_for_update().get(pk=stage.pk)
            run = locked.run
            if (
                run.status not in ACTIVE
                or locked.status != ReviewStage.RUNNING
                or locked.lease_expires_at > now
            ):
                continue
            locked.status = (
                ReviewStage.PENDING
                if locked.attempts < 3 and locked.manual_retries == 0
                else ReviewStage.ERROR
            )
            locked.error_category = "worker_lost"
            locked.reason = _("The review worker did not finish this section.")
            locked.lease_token = None
            locked.lease_expires_at = None
            locked.save()
            enqueue(locked.kind, run.pk)
    # Notification recovery is independent of review/credit execution. Bound
    # batches keep the periodic task cheap if a service outage creates a backlog.
    for kind, model in (("problem", ProblemReviewRun), ("contest", ContestReviewRun)):
        for run_id in model.objects.filter(
            workflow_version=2,
            status=model.DONE,
            notifications_sent=False,
        ).values_list("pk", flat=True)[:100]:
            try:
                emit_completion.apply_async(
                    args=[kind, run_id],
                    queue=getattr(
                        settings, "REVIEW_COORDINATOR_QUEUE", "review-coordinator"
                    ),
                )
            except Exception:
                logger.exception(
                    "Coordinator notification delivery remains pending (%s #%s)",
                    kind,
                    run_id,
                )
