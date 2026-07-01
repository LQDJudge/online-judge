"""Celery task for executing the auto-review pipeline."""

import logging
import traceback
from datetime import datetime, timedelta, timezone

from celery import shared_task
from django.conf import settings
from django.urls import reverse
from django.utils import translation

from judge.models import Profile
from judge.models.notification import Notification, NotificationCategory
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.prompts import SYNTHESIS_SYSTEM
from judge.review.registry import CHECKS
from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


def _call_llm_text(system_prompt: str, user_prompt: str) -> str:
    """Plain text LLM call (used by synthesis — no JSON wrapping needed)."""
    config = get_config()
    service = LLMService(
        api_key=config.api_key, bot_name=config.get_bot_name_for_review()
    )
    return service.call_llm(user_prompt, system_prompt=system_prompt) or ""


@shared_task(bind=True)
def review_problem(self, run_id):
    """
    Execute every check in CHECKS against the ProblemReviewRun's problem, writing
    one ProblemReviewCheckResult row per check. Marks the run Done at the end.
    If the runner itself crashes outside per-check handling, mark the run as
    ERROR and notify so it doesn't stay stuck in RUNNING (the reaper would
    eventually pick it up, but explicit handling is faster + cleaner).
    """
    # Celery tasks run outside the HTTP request cycle, so Django's locale
    # middleware never fires. Without an explicit activate(), gettext() falls
    # back to the source English strings even when LANGUAGE_CODE is "vi".
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))

    try:
        run = ProblemReviewRun.objects.select_related("problem").get(id=run_id)
    except ProblemReviewRun.DoesNotExist:
        logger.error("review_problem: ProblemReviewRun %s not found", run_id)
        return {"success": False, "error": "run not found"}

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

        _emit_review_done_notifications(run)
        return {"success": True, "run_id": run.id}
    except Exception as exc:
        # Runner-level crash (e.g., DB connection drop mid-loop, unhandled
        # exception in the registry iteration itself). Don't leave the run
        # stuck in RUNNING. Mark ERROR and notify both author and admins so
        # someone can take action — silent failure is the worst outcome.
        logger.exception("review_problem crashed for run %s", run_id)
        ProblemReviewRun.objects.filter(id=run_id).update(
            status=ProblemReviewRun.ERROR,
            finished_at=datetime.now(timezone.utc),
        )
        try:
            run.refresh_from_db()
            _emit_review_error_notifications(run, reason=str(exc))
        except Exception:
            logger.exception("Failed to emit error notifications for run %s", run_id)
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

    try:
        return _call_llm_text(SYNTHESIS_SYSTEM, user_prompt)
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
        run = ProblemReviewRun.objects.select_related("problem").get(id=run_id)
    except ProblemReviewRun.DoesNotExist:
        return {"success": False, "error": "run not found"}

    run.summary_report = _run_synthesis_inline(run)
    run.save(update_fields=["summary_report"])
    return {"success": True}


def _emit_review_done_notifications(run):
    """Notify the author + superusers that the run finished.

    Author and admin see DIFFERENT html_links: the author's link goes only
    to the review dashboard (where they have permission); admins additionally
    see a "(Review)" suffix linking to the admin queue (where only they have
    permission). Reusing one html_link for both audiences was the original
    bug — author-side notifications contained an admin-only link that 404'd.
    """
    problem = run.problem
    try:
        dashboard_url = reverse("problem_review_dashboard", args=[problem.code])
    except Exception:
        dashboard_url = f"/problem/{problem.code}/review/"
    try:
        queue_url = reverse("internal_problem_queue") + "?tab=request_public&status=P"
    except Exception:
        queue_url = "/internal/queue/?tab=request_public&status=P"

    author_html_link = '<a href="%(dashboard)s">%(name)s</a>' % {
        "dashboard": dashboard_url,
        "name": problem.name,
    }
    admin_html_link = (
        '<a href="%(dashboard)s">%(name)s</a>' ' (<a href="%(queue)s">%(review)s</a>)'
    ) % {
        "dashboard": dashboard_url,
        "name": problem.name,
        "queue": queue_url,
        "review": "Review",
    }

    if run.triggered_by:
        Notification.objects.create_notification(
            owner=run.triggered_by,
            category=NotificationCategory.PUBLIC_REQUEST_REVIEW_DONE,
            html_link=author_html_link,
            author=None,
        )

    superuser_profiles = Profile.objects.filter(user__is_superuser=True)
    if run.triggered_by:
        superuser_profiles = superuser_profiles.exclude(id=run.triggered_by.id)
    for profile in superuser_profiles:
        Notification.objects.create_notification(
            owner=profile,
            category=NotificationCategory.PUBLIC_REQUEST_NEW,
            html_link=admin_html_link,
            author=run.triggered_by,
        )


def _emit_review_error_notifications(run, reason=""):
    """Notify author + admins that a review run errored (status=E).

    Symmetric with _emit_review_done_notifications but uses the dedicated
    PUBLIC_REQUEST_REVIEW_ERROR category. Called from the runner exception
    path and the reaper when a stuck run is timed out — both cases need
    the user to know something went wrong rather than fail silently.
    """
    problem = run.problem
    try:
        dashboard_url = reverse("problem_review_dashboard", args=[problem.code])
    except Exception:
        dashboard_url = f"/problem/{problem.code}/review/"

    html_link = '<a href="%(dashboard)s">%(name)s</a>' % {
        "dashboard": dashboard_url,
        "name": problem.name,
    }

    if run.triggered_by:
        Notification.objects.create_notification(
            owner=run.triggered_by,
            category=NotificationCategory.PUBLIC_REQUEST_REVIEW_ERROR,
            html_link=html_link,
            author=None,
        )

    superuser_profiles = Profile.objects.filter(user__is_superuser=True)
    if run.triggered_by:
        superuser_profiles = superuser_profiles.exclude(id=run.triggered_by.id)
    for profile in superuser_profiles:
        Notification.objects.create_notification(
            owner=profile,
            category=NotificationCategory.PUBLIC_REQUEST_REVIEW_ERROR,
            html_link=html_link,
            author=run.triggered_by,
        )


@shared_task(bind=True)
def reap_stale_review_runs(self):
    """Mark any ProblemReviewRun that has been Running too long as Error.

    Also notifies author + admins so the author isn't left waiting on a run
    that silently timed out. Without notification, the dashboard's polling
    JS keeps spinning indefinitely with no signal.
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))
    timeout = getattr(settings, "AUTO_REVIEW_RUN_TIMEOUT_SECONDS", 1800)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=timeout)
    qs = ProblemReviewRun.objects.filter(
        status=ProblemReviewRun.RUNNING, started_at__lt=cutoff
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
