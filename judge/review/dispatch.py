"""Guarded broker delivery; provider attempts are never delivery attempts."""

import logging
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from celery import current_app

from judge.models import ContestReviewRun, ProblemReviewRun, ReviewStage

logger = logging.getLogger(__name__)
_inline_scheduler = ContextVar("review_inline_scheduler", default=False)
ACTIVE = ("R", "B")


def _model(kind):
    if kind not in ("problem", "contest"):
        raise ValueError("Unknown review kind")
    return ProblemReviewRun if kind == "problem" else ContestReviewRun


def record_delivery_failure(kind, run_id, stage_id=None):
    with transaction.atomic():
        run = _model(kind).objects.select_for_update().get(pk=run_id)
        if run.status not in ACTIVE:
            return
        stages = run.coordinator_stages.select_for_update()
        if stage_id:
            stage = stages.filter(stage_id=stage_id).first()
        else:
            stage = stages.filter(status=ReviewStage.PENDING).order_by("pk").first()
            if stage is None and not stages.exists():
                stage = ReviewStage.objects.create(**{kind + "_run": run}, stage_id="D")
        if stage is None or stage.status != ReviewStage.PENDING:
            # Completed outputs/claims are immutable even when finalization
            # delivery is lost. The periodic scheduler can finalize locally.
            if (
                stage_id is None
                and not stages.exclude(status=ReviewStage.DONE).exists()
            ):
                run.status = run.BLOCKED
                run.readiness = "INCOMPLETE"
                run.save(update_fields=["status", "readiness"])
            return
        if (
            stage.error_category == "broker_delivery"
            and stage.retry_at
            and stage.retry_at > timezone.now()
        ):
            return
        stage.delivery_attempts += 1
        stage.error_category = "broker_delivery"
        stage.reason = _("This section could not be delivered to the review worker.")
        retry = stage.delivery_attempts < 3 and stage.manual_retries == 0
        stage.status = ReviewStage.PENDING if retry else ReviewStage.ERROR
        stage.retry_at = (
            timezone.now() + timedelta(seconds=5 * 2 ** (stage.delivery_attempts - 1))
            if retry
            else None
        )
        stage.save(
            update_fields=[
                "delivery_attempts",
                "error_category",
                "reason",
                "status",
                "retry_at",
            ]
        )
        if not retry:
            run.status = run.BLOCKED
            run.readiness = "INCOMPLETE"
            run.save(update_fields=["status", "readiness"])


def dispatch_stage(kind, run_id, stage_id, countdown=0):
    if not _model(kind).objects.filter(pk=run_id, status__in=ACTIVE).exists():
        return
    stage = ReviewStage.objects.filter(
        **{kind + "_run_id": run_id}, stage_id=stage_id
    ).first()
    if stage is None or stage.status != ReviewStage.PENDING:
        return
    if (
        stage.error_category == "broker_delivery"
        and stage.retry_at
        and stage.retry_at > timezone.now()
    ):
        return
    if stage.retry_at:
        countdown = max(countdown, (stage.retry_at - timezone.now()).total_seconds(), 0)
    try:
        current_app.send_task(
            "judge.tasks.coordinator.run_review_stage",
            args=[kind, run_id, stage_id],
            countdown=countdown,
            queue=getattr(settings, "REVIEW_COORDINATOR_QUEUE", "review-coordinator"),
        )
    except Exception:
        logger.exception(
            "Coordinator stage broker delivery failed (%s #%s %s)",
            kind,
            run_id,
            stage_id,
        )
        record_delivery_failure(kind, run_id, stage_id)
        return
    # Clear only our dispatch error, never provider/judge errors or counters.
    with transaction.atomic():
        run = _model(kind).objects.select_for_update().get(pk=run_id)
        changed = ReviewStage.objects.filter(
            pk=stage.pk, status=ReviewStage.PENDING, error_category="broker_delivery"
        ).update(error_category="", reason="", retry_at=None)
        if (
            changed
            and run.status == run.BLOCKED
            and not run.coordinator_stages.filter(status=ReviewStage.ERROR).exists()
        ):
            child_blocked = (
                kind == "contest"
                and run.child_dependencies.filter(problem_run__status="B").exists()
            )
            if not child_blocked:
                run.status = run.RUNNING
                run.readiness = ""
                run.save(update_fields=["status", "readiness"])


def dispatch_advance(kind, run_id, countdown=0):
    try:
        current_app.send_task(
            "judge.tasks.coordinator.advance_review",
            args=[kind, run_id],
            countdown=countdown,
            queue=getattr(settings, "REVIEW_COORDINATOR_QUEUE", "review-coordinator"),
        )
    except Exception:
        logger.exception(
            "Coordinator scheduler broker delivery failed (%s #%s)", kind, run_id
        )
        # This fallback executes only the DB scheduler, never a model or judge
        # stage. Waiting-contest countdowns/reentrant callbacks must not recurse.
        scheduler = current_app.tasks.get("judge.tasks.coordinator.advance_review")
        if scheduler is not None and not countdown and not _inline_scheduler.get():
            token = _inline_scheduler.set(True)
            try:
                scheduler.run(kind, run_id)
                return
            except Exception:
                logger.exception(
                    "Local coordinator scheduler fallback failed (%s #%s)", kind, run_id
                )
            finally:
                _inline_scheduler.reset(token)
        record_delivery_failure(kind, run_id)
