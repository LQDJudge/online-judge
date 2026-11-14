"""Execute one claimed phase at a time; no dependency polling or raw call archive."""

import logging
from datetime import timedelta
from functools import partial

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from judge.models import (
    ContestReviewDependency,
    ContestReviewRun,
    Problem,
    ProblemReviewRun,
    ReviewStage,
)
from judge.models.notification import Notification, NotificationCategory
from judge.auto_review.phases import (
    build_stage_input,
    execute_stage,
    CoordinatorOutputError,
)
from judge.auto_review.test_statistics import (
    dispatch_extraction,
    valid_extraction_result,
    MAX_SOURCE_BYTES,
)
from judge.auto_review.review_context import current_content_hash
from judge.auto_review.scheduling import (
    run_model,
    schedule_phases,
    notify_parents,
    definitions_for_run,
)
from judge.auto_review.workflow import (
    contest_report_readable,
    invalidate_readiness,
    cancel_review,
)

logger = logging.getLogger(__name__)


def _accept_identity(run):
    obj = run.problem if hasattr(run, "problem_id") else run.contest
    return run.input_hash == current_content_hash(obj)


def _finish(kind, run_id, key, token, output=None, error=None):
    model = run_model(kind)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        stage = run.coordinator_stages.filter(
            stage_id=key, status=ReviewStage.RUNNING, lease_token=token
        ).first()
        if run.status not in ("R", "B") or not stage:
            return
        if not _accept_identity(run):
            cancel_review(run, run.triggered_by, _owned_cascade=True)
            return
        if error is not None:
            stage.error_category = getattr(error, "error_category", "infrastructure")
            stage.reason = _("This section encountered an infrastructure issue.")
            stage.error_excerpt = getattr(error, "raw_response", "")[:8192]
            stage.diagnostics_expires_at = (
                timezone.now() + timedelta(days=7) if stage.error_excerpt else None
            )
            retry = (
                stage.attempts < 3
                and stage.manual_retries == 0
                and getattr(error, "retryable", True)
            )
            delay = getattr(error, "retry_after", 0) or 0
            if not isinstance(delay, (int, float)) or delay > 3600 or delay < 0:
                retry = False
                delay = 0
            stage.status = ReviewStage.PENDING if retry else ReviewStage.ERROR
            stage.retry_at = (
                timezone.now()
                + timedelta(seconds=max(15 * 2 ** (stage.attempts - 1), delay))
                if retry
                else None
            )
            if key == "extract_stats" and not retry:
                stage.status = ReviewStage.DONE
                stage.output_json = {
                    "availability": "unavailable",
                    "limitations": [_("Test statistics could not be extracted.")],
                }
        else:
            stage.status, stage.output_json = ReviewStage.DONE, output
            stage.error_category, stage.reason, stage.error_excerpt = "", "", ""
            stage.retry_at = None
        stage.finished_at = (
            timezone.now()
            if stage.status in (ReviewStage.DONE, ReviewStage.ERROR)
            else None
        )
        stage.save()
        next_key = "__retry__" + key if stage.status == ReviewStage.PENDING else key
        transaction.on_commit(partial(schedule_phases, kind, run_id, next_key))


@shared_task
def run_review_stage(kind, run_id, stage_id, attempt_token):
    model = run_model(kind)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        stage = run.coordinator_stages.filter(
            stage_id=stage_id, status=ReviewStage.QUEUED, lease_token=attempt_token
        ).first()
        if not stage or run.status not in ("R", "B"):
            return
        stage.status, stage.started_at = ReviewStage.RUNNING, timezone.now()
        stage.attempts += 1
        stage.save(update_fields=["status", "started_at", "attempts"])
        run._review_stage_claim = (stage.pk, stage.lease_token)
    try:
        if kind == "contest" and (
            not run.triggered_by_id
            or not contest_report_readable(run, run.triggered_by.user)
        ):
            error = CoordinatorOutputError(
                _("A linked problem review is no longer accessible.")
            )
            error.retryable = False
            raise error
        if stage_id == "extract_stats":
            contract = run.coordinator_stages.get(
                stage_id="problem_contract"
            ).output_json
            helper = contract.get("evidence", {}).get("helper_source")
            try:
                usable = (
                    isinstance(helper, str)
                    and bool(helper.strip())
                    and len(helper.encode("utf8")) <= MAX_SOURCE_BYTES
                )
            except UnicodeError:
                usable = False
            if not usable:
                _finish(
                    kind,
                    run_id,
                    stage_id,
                    attempt_token,
                    {
                        "availability": "unavailable",
                        "limitations": [_("No usable statistics helper was provided.")],
                    },
                )
                return
            dispatch_extraction(stage, helper)
            return
        inputs = build_stage_input(run, stage_id)
        result = execute_stage(run, stage_id, inputs)
    except Exception as error:
        logger.exception(
            "Review phase failed kind=%s run=%s phase=%s attempt=%s",
            kind,
            run_id,
            stage_id,
            attempt_token,
        )
        _finish(kind, run_id, stage_id, attempt_token, error=error)
    else:
        _finish(kind, run_id, stage_id, attempt_token, result)


@shared_task
def complete_extraction(stage_id, attempt_token, result):
    stage = (
        ReviewStage.objects.select_related("problem_run")
        .filter(
            pk=stage_id,
            stage_id="extract_stats",
            status=ReviewStage.RUNNING,
            lease_token=attempt_token,
        )
        .first()
    )
    if not stage or not valid_extraction_result(result, stage_id, attempt_token):
        return
    if result.get("status") == "infrastructure_error":
        _finish(
            "problem",
            stage.problem_run_id,
            "extract_stats",
            attempt_token,
            error=OSError("Judge extraction interrupted"),
        )
        return
    cases = result.get("cases", [])
    output = {
        "availability": (
            "available"
            if result.get("status") == "completed"
            and any(c.get("status") == "ok" for c in cases)
            else "unavailable"
        ),
        "cases": cases,
        "limitations": (
            []
            if result.get("status") == "completed"
            else [_("Test statistics could not be extracted.")]
        ),
    }
    _finish("problem", stage.problem_run_id, "extract_stats", attempt_token, output)


@shared_task
def refresh_review_freshness(problem_id):
    problem = Problem.objects.filter(pk=problem_id).first()
    if problem:
        # A completed rejudge can change evidence without a Content Save. Only
        # meaningful identity changes cancel active work; identical results do not.
        current = current_content_hash(problem)
        for run in (
            ProblemReviewRun.objects.filter(problem=problem, status__in=("R", "B"))
            .exclude(input_hash=current)
            .order_by("pk")
        ):
            cancel_review(run, None, _owned_cascade=True)
        parent_ids = (
            ContestReviewDependency.objects.filter(problem=problem)
            .exclude(content_hash=current)
            .values_list("contest_run_id", flat=True)
        )
        for run in ContestReviewRun.objects.filter(
            pk__in=parent_ids, status__in=("R", "B")
        ).order_by("pk"):
            cancel_review(run, None, _owned_cascade=True)
        invalidate_readiness(problem)


@shared_task
def emit_completion(kind, run_id):
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
def emit_recovery(kind, run_id):
    model = run_model(kind)
    with transaction.atomic():
        run = model.objects.select_for_update().get(pk=run_id)
        if run.status != model.BLOCKED:
            return
        stages = list(
            run.coordinator_stages.filter(
                status=ReviewStage.ERROR, recovery_notified=False
            )
        )
        if not stages:
            return
        obj = run.problem if kind == "problem" else run.contest
        emit_review_notification(kind, obj, run.pk, readiness="INCOMPLETE")
        ReviewStage.objects.filter(pk__in=[s.pk for s in stages]).update(
            recovery_notified=True
        )


@shared_task
def cleanup_review_diagnostics():
    return ReviewStage.objects.filter(
        diagnostics_expires_at__lte=timezone.now()
    ).update(error_excerpt="", diagnostics_expires_at=None)


def reap_review_runs(kind):
    model = run_model(kind)
    # A broker outage may have prevented the initial notification from being queued.
    for run_id in (
        model.objects.filter(
            status=model.BLOCKED,
            coordinator_stages__status=ReviewStage.ERROR,
            coordinator_stages__recovery_notified=False,
        )
        .values_list("pk", flat=True)
        .distinct()
    ):
        emit_recovery.delay(kind, run_id)
    timeout = getattr(
        settings,
        (
            "AUTO_REVIEW_CONTEST_RUN_TIMEOUT_SECONDS"
            if kind == "contest"
            else "AUTO_REVIEW_RUN_TIMEOUT_SECONDS"
        ),
        3600,
    )
    cutoff = timezone.now() - timedelta(seconds=timeout)
    for identity in model.objects.filter(
        status=model.RUNNING, started_at__lt=cutoff
    ).values_list("pk", flat=True):
        with transaction.atomic():
            run = model.objects.select_for_update().get(pk=identity)
            if run.status != model.RUNNING:
                continue
            # A contest merely waiting on active children has no lost work of its own.
            if (
                kind == "contest"
                and run.child_dependencies.filter(
                    problem_run__status__in=("R", "B")
                ).exists()
            ):
                continue
            phases = {phase.stage_id: phase for phase in run.coordinator_stages.all()}
            # Use persisted progress rather than killing a healthy long chain
            # just because its admission time is old. No per-phase watchdog.
            activity = [
                stamp
                for phase in phases.values()
                for stamp in (phase.started_at, phase.finished_at, phase.retry_at)
                if stamp
            ]
            if activity and max(activity) >= cutoff:
                continue
            if phases and all(
                phase.status == ReviewStage.DONE for phase in phases.values()
            ):
                transaction.on_commit(partial(schedule_phases, kind, identity))
                continue
            if not any(
                phase.status in (ReviewStage.RUNNING, ReviewStage.QUEUED)
                for phase in phases.values()
            ):
                definitions = definitions_for_run(run)
                missing_dispatch = next(
                    (
                        phase
                        for key, phase in phases.items()
                        if phase.status == ReviewStage.PENDING
                        and key in definitions
                        and all(
                            phases.get(dep) and phases[dep].status == ReviewStage.DONE
                            for dep in definitions[key]["dependencies"]
                        )
                    ),
                    None,
                )
                if missing_dispatch:
                    missing_dispatch.status = ReviewStage.QUEUED
                    missing_dispatch.save(update_fields=["status"])
            optional = run.coordinator_stages.filter(
                stage_id="extract_stats",
                status__in=(ReviewStage.RUNNING, ReviewStage.QUEUED),
            ).update(
                status=ReviewStage.DONE,
                output_json={"availability": "unavailable"},
                finished_at=timezone.now(),
                lease_token=None,
            )
            if optional:
                transaction.on_commit(
                    partial(schedule_phases, kind, identity, "extract_stats")
                )
            if (
                optional
                and not run.coordinator_stages.filter(
                    status__in=(ReviewStage.RUNNING, ReviewStage.QUEUED)
                ).exists()
            ):
                continue
            run.coordinator_stages.filter(
                status__in=(ReviewStage.RUNNING, ReviewStage.QUEUED)
            ).update(
                status=ReviewStage.ERROR,
                lease_token=None,
                error_category="worker_timeout",
                reason=_(
                    "The review worker did not finish in time. Retry this section."
                ),
            )
            run.status, run.readiness = model.BLOCKED, "INCOMPLETE"
            run.save(update_fields=["status", "readiness"])
            notify_parents(run)
            transaction.on_commit(partial(emit_recovery.delay, kind, identity))
