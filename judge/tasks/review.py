"""Celery task for executing the auto-review pipeline."""

import logging
import traceback
from datetime import datetime, timedelta, timezone

from celery import shared_task
from django.conf import settings
from django.utils import translation

from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.prompts import SYNTHESIS_SYSTEM
from judge.review.registry import CHECKS
from judge.tasks.coordinator import advance_review, emit_completion
from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


def _call_llm_text(system_prompt: str, user_prompt: str, user_id=None) -> str:
    """Plain text LLM call (used by synthesis — no JSON wrapping needed)."""
    config = get_config()
    service = LLMService(
        api_key=config.api_key,
        bot_name=config.get_bot_name_for_review(),
        feature="problem_review_synthesis",
        user_id=user_id,
    )
    return service.call_llm(user_prompt, system_prompt=system_prompt) or ""


@shared_task(bind=True)
def review_problem(self, run_id, emit_notifications=True):
    """
    Execute every check in CHECKS against the ProblemReviewRun's problem, writing
    one ProblemReviewCheckResult row per check. Marks the run Done at the end.
    If the runner itself crashes outside per-check handling, mark the run as
    ERROR so it doesn't stay stuck in RUNNING (the reaper would
    eventually pick it up, but explicit handling is faster + cleaner).

    emit_notifications: legacy contest-triggered child reviews can suppress
    per-problem completion notifications. Otherwise only current authors hear
    when the review finishes; superusers hear about publication requests instead.
    """
    # Celery tasks run outside the HTTP request cycle, so Django's locale
    # middleware never fires. Without an explicit activate(), gettext() falls
    # back to the source English strings even when LANGUAGE_CODE is "vi".
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))

    try:
        run = ProblemReviewRun.objects.select_related("problem", "triggered_by").get(
            id=run_id
        )
    except ProblemReviewRun.DoesNotExist:
        logger.error("review_problem: ProblemReviewRun %s not found", run_id)
        return {"success": False, "error": "run not found"}

    if run.workflow_version == 2:
        return advance_review("problem", run_id)

    # Idempotency guard for the common redelivery case. Celery delivers
    # at-least-once, so this task can be handed the same run_id more than once —
    # most often a backlog drain after downtime or a duplicate enqueue, where
    # the run has already reached DONE. Re-running a DONE run would collide on
    # the (run, check_id) unique constraint AND re-spend LLM tokens, so treat it
    # as a no-op. NOTE: a redelivery that races a still-RUNNING run is not
    # deduped here — update_or_create below keeps that case from crashing, but
    # it may double-emit notifications; a stronger claim/lock is a follow-up.
    if run.status == ProblemReviewRun.DONE:
        logger.info("review_problem: run %s already DONE, ignoring redelivery", run_id)
        return {"success": True, "run_id": run.id, "idempotent": True}

    try:
        problem = run.problem

        for check in CHECKS:
            # update_or_create (not create): a prior attempt that crashed
            # mid-loop leaves the run in RUNNING with some rows already
            # written. A redelivered task must overwrite those rows rather
            # than collide with create() on the (run, check_id) constraint.
            result, _created = ProblemReviewCheckResult.objects.update_or_create(
                run=run,
                check_id=check.id,
                defaults={
                    "status": ProblemReviewCheckResult.PENDING,
                    "started_at": datetime.now(timezone.utc),
                    "reason": "",
                    "details_json": {},
                    "finished_at": None,
                },
            )
            try:
                data = check.run(problem, run)
                result.status = data.status
                result.reason = data.reason or ""
                result.details_json = data.details or {}
            except Exception as exc:
                logger.exception("Check %s crashed for run %s", check.id, run_id)
                result.status = ProblemReviewCheckResult.ERROR
                result.reason = f"Check errored: {exc}"
                result.details_json = {"traceback": traceback.format_exc()}
            result.finished_at = datetime.now(timezone.utc)
            result.save()

        # Synthesis runs INLINE (not as a separate Celery message) so that by the
        # time we flip status -> DONE, summary_report is guaranteed populated.
        # The dashboard's polling JS treats status==DONE as "fully ready"; if we
        # marked DONE before synthesis, the JS would reload and show a stale
        # "no AI feedback" placeholder until the user F5'd a second time.
        run.summary_report = _run_synthesis_inline(run)

        run.status = ProblemReviewRun.DONE
        run.finished_at = datetime.now(timezone.utc)
        run.save(update_fields=["status", "finished_at", "summary_report"])

        if emit_notifications:
            _emit_review_done_notifications(run)
        return {"success": True, "run_id": run.id}
    except Exception as exc:
        # Runner-level crash (e.g., DB connection drop mid-loop, unhandled
        # exception in the registry iteration itself). Don't leave the run
        # stuck in RUNNING. Keep ERROR visible in the report and operational logs.
        logger.exception("review_problem crashed for run %s", run_id)
        ProblemReviewRun.objects.filter(id=run_id).update(
            status=ProblemReviewRun.ERROR,
            finished_at=datetime.now(timezone.utc),
        )
        if emit_notifications:
            try:
                run.refresh_from_db()
                _emit_review_error_notifications(run, reason=str(exc))
            except Exception:
                logger.exception(
                    "Failed to emit error notifications for run %s", run_id
                )
        return {"success": False, "error": str(exc)}


def _run_synthesis_inline(run):
    """Return the synthesis markdown for `run`, or "" if there's nothing to synthesize.

    Failures are caught and folded into the returned text — synthesis must
    never raise out of review_problem (which would leave the run stuck in R).
    """
    results = list(run.check_results.all())
    failed = [r for r in results if r.status == ProblemReviewCheckResult.FAIL]
    if not failed:
        return ""

    blob = []
    for r in results:
        if r.status == ProblemReviewCheckResult.FAIL:
            blob.append(
                f"## {r.check_id} — FAIL\nReason: {r.reason}\nDetails: {r.details_json}"
            )
        elif r.status == ProblemReviewCheckResult.ERROR:
            blob.append(f"## {r.check_id} — ERROR\n{r.reason}")

    user_prompt = (
        f"Problem: {run.problem.name}\n\n"
        f"Auto-review results:\n\n" + "\n\n".join(blob)
    )

    user_id = run.triggered_by.user_id if run.triggered_by_id else None

    try:
        return _call_llm_text(SYNTHESIS_SYSTEM, user_prompt, user_id=user_id)
    except Exception as exc:
        logger.exception("Synthesis failed for run %s", run.id)
        return f"_(Synthesis unavailable: {exc})_\n\nRaw failures:\n\n" + "\n".join(
            blob[:3]
        )


@shared_task(bind=True)
def synthesize_feedback(self, run_id):
    """Re-generate the synthesis for an existing run.

    Kept as a standalone task for the "re-synthesize without re-running checks"
    affordance (admin tool / partial retry). The primary review pipeline calls
    _run_synthesis_inline directly so summary_report lands atomically with the
    DONE status flip.
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))
    try:
        run = ProblemReviewRun.objects.select_related("problem", "triggered_by").get(
            id=run_id
        )
    except ProblemReviewRun.DoesNotExist:
        return {"success": False, "error": "run not found"}

    run.summary_report = _run_synthesis_inline(run)
    run.save(update_fields=["summary_report"])
    return {"success": True}


def _emit_review_done_notifications(run):
    """Notify current authors once; publication requests notify superusers separately."""
    emit_completion("problem", run.pk)


def _emit_review_error_notifications(run, reason=""):
    """Keep infrastructure failures visible in logs and the review, not notifications."""
    logger.warning(
        "problem review %s needs infrastructure recovery: %s", run.pk, reason
    )


@shared_task(bind=True)
def reap_stale_review_runs(self):
    """Mark any ProblemReviewRun that has been Running too long as Error.

    Infrastructure failures stay visible in the report and operational logs,
    without sending repeated author or administrator notifications.
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))
    timeout = getattr(settings, "AUTO_REVIEW_RUN_TIMEOUT_SECONDS", 1800)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=timeout)
    qs = ProblemReviewRun.objects.filter(
        workflow_version=0, status=ProblemReviewRun.RUNNING, started_at__lt=cutoff
    )

    # Capture the runs about to be marked ERROR so we can notify per-run.
    # We re-fetch after the update to avoid a race with the writer, but
    # since this is the only path that flips R→E for stale runs, the
    # snapshot is safe in practice.
    stale_runs = list(qs.select_related("problem", "triggered_by"))
    count = qs.update(status=ProblemReviewRun.ERROR, finished_at=now)
    for run in stale_runs:
        run.status = ProblemReviewRun.ERROR
        run.finished_at = now
        try:
            _emit_review_error_notifications(run, reason="Run timed out")
        except Exception:
            logger.exception(
                "Failed to emit reaper error notifications for run %s", run.id
            )
    return {"reaped": count}
