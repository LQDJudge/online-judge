"""Celery tasks for the contest auto-review pipeline.

Mirrors `judge/tasks/review.py` for problems. Major shape differences:

  - Iterates the CONTEST_CHECKS registry (currently empty until CR-10..CR-13
    populate it).
  - Writes ContestReviewCheckResult rows, whose status enum adds WARNING on
    top of the problem-side SUCCESS/FAIL/SKIPPED/ERROR set.
  - Uses the CONTEST_PUBLIC_REQUEST_REVIEW_{DONE,ERROR} notification
    categories so author + admin filters can pick them out from the problem
    review categories.

Synthesis is currently a placeholder pass-through; CR-13 will replace
`_run_synthesis_inline` with the real Vietnamese-prose generation.
"""

import logging
import traceback
from datetime import datetime, timedelta, timezone

from celery import shared_task
from django.conf import settings
from django.urls import reverse
from django.utils import translation

from judge.models import Profile
from judge.models.contest_review import ContestReviewCheckResult, ContestReviewRun
from judge.models.notification import Notification, NotificationCategory
from judge.review.contest_registry import CONTEST_CHECKS
from judge.review.prompts import CONTEST_SYNTHESIS_SYSTEM
from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


def _call_llm_text(system_prompt: str, user_prompt: str) -> str:
    """Plain text LLM call (used by contest synthesis — no JSON wrapping needed).

    Mirrors `_call_llm_text` from judge/tasks/review.py. Kept local to this
    module so each task's LLM usage stays explicit at the call site.
    """
    config = get_config()
    service = LLMService(
        api_key=config.api_key, bot_name=config.get_bot_name_for_review()
    )
    return service.call_llm(user_prompt, system_prompt=system_prompt) or ""


@shared_task(bind=True)
def review_contest(self, run_id):
    """
    Execute every check in CONTEST_CHECKS against the run's contest, writing
    one ContestReviewCheckResult row per check. Marks the run Done at the end.
    Runner-level crashes mark the run as ERROR + notify so it doesn't stay
    stuck in RUNNING (the reaper would eventually pick it up, but explicit
    handling is faster + cleaner).
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))

    try:
        run = ContestReviewRun.objects.select_related("contest").get(id=run_id)
    except ContestReviewRun.DoesNotExist:
        logger.error("review_contest: ContestReviewRun %s not found", run_id)
        return {"success": False, "error": "run not found"}

    try:
        contest = run.contest

        for check in CONTEST_CHECKS:
            result = ContestReviewCheckResult.objects.create(
                run=run,
                check_id=check.id,
                status=ContestReviewCheckResult.PENDING,
                started_at=datetime.now(timezone.utc),
            )
            try:
                data = check.run(contest, run)
                result.status = data.status
                result.reason = data.reason or ""
                result.details_json = data.details or {}
            except Exception as exc:
                logger.exception(
                    "Check %s crashed for contest run %s", check.id, run_id
                )
                result.status = ContestReviewCheckResult.ERROR
                result.reason = f"Check errored: {exc}"
                result.details_json = {"traceback": traceback.format_exc()}
            result.finished_at = datetime.now(timezone.utc)
            result.save()

        # Synthesis runs INLINE (not as a separate Celery message) so that by
        # the time we flip status -> DONE, summary_report is guaranteed
        # populated. Dashboard polling treats status==DONE as "fully ready".
        run.summary_report = _run_synthesis_inline(run)

        run.status = ContestReviewRun.DONE
        run.finished_at = datetime.now(timezone.utc)
        run.save(update_fields=["status", "finished_at", "summary_report"])

        _emit_contest_review_done_notifications(run)
        return {"success": True, "run_id": run.id}
    except Exception as exc:
        # Runner-level crash. Don't leave the run stuck in RUNNING.
        logger.exception("review_contest crashed for run %s", run_id)
        ContestReviewRun.objects.filter(id=run_id).update(
            status=ContestReviewRun.ERROR,
            finished_at=datetime.now(timezone.utc),
        )
        try:
            run.refresh_from_db()
            _emit_contest_review_error_notifications(run, reason=str(exc))
        except Exception:
            logger.exception(
                "Failed to emit error notifications for contest run %s", run_id
            )
        return {"success": False, "error": str(exc)}


def _run_synthesis_inline(run):
    """Return the synthesis markdown for `run`, or "" if there's nothing to say.

    Failures are caught and folded into the returned text — synthesis must
    NEVER raise out of review_contest (which would leave the run stuck in R).

    Sends FAIL + WARNING + ERROR check results to the LLM with their reason
    and details. PASS / SKIPPED rows are omitted since the dashboard table
    already shows them. Caller (review_contest) writes the returned string
    into ContestReviewRun.summary_report.
    """
    results = list(run.check_results.all())
    notable = [
        r
        for r in results
        if r.status
        in (
            ContestReviewCheckResult.FAIL,
            ContestReviewCheckResult.WARNING,
            ContestReviewCheckResult.ERROR,
        )
    ]
    if not notable:
        return ""

    blob = []
    for r in notable:
        blob.append(
            f"## {r.check_id} — {r.get_status_display()}\n"
            f"Reason: {r.reason}\n"
            f"Details: {r.details_json}"
        )

    user_prompt = (
        f"Contest: {run.contest.name} (key: {run.contest.key})\n\n"
        f"Auto-review results:\n\n" + "\n\n".join(blob)
    )

    try:
        return _call_llm_text(CONTEST_SYNTHESIS_SYSTEM, user_prompt)
    except Exception as exc:
        logger.exception("Contest synthesis failed for run %s", run.id)
        return f"_(Synthesis unavailable: {exc})_\n\nRaw findings:\n\n" + "\n".join(
            blob[:3]
        )


@shared_task(bind=True)
def synthesize_contest_feedback(self, run_id):
    """Re-generate the synthesis for an existing contest run.

    Kept as a standalone task for the "re-synthesize without re-running checks"
    affordance (admin tool / partial retry). The primary review pipeline calls
    _run_synthesis_inline directly.
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))
    try:
        run = ContestReviewRun.objects.select_related("contest").get(id=run_id)
    except ContestReviewRun.DoesNotExist:
        return {"success": False, "error": "run not found"}

    run.summary_report = _run_synthesis_inline(run)
    run.save(update_fields=["summary_report"])
    return {"success": True}


def _emit_contest_review_done_notifications(run):
    """Notify author + superusers that the contest run finished.

    Mirrors `_emit_review_done_notifications` from the problem pipeline,
    using CONTEST_* notification categories so users can filter contest
    reviews separately from problem reviews.
    """
    contest = run.contest
    try:
        dashboard_url = reverse("contest_review_dashboard", args=[contest.key])
    except Exception:
        dashboard_url = f"/contest/{contest.key}/review/"
    try:
        queue_url = (
            reverse("internal_problem_queue") + "?tab=contest_request_public&status=P"
        )
    except Exception:
        queue_url = "/internal/queue/?tab=contest_request_public&status=P"

    author_html_link = '<a href="%(dashboard)s">%(name)s</a>' % {
        "dashboard": dashboard_url,
        "name": contest.name,
    }
    admin_html_link = (
        '<a href="%(dashboard)s">%(name)s</a>' ' (<a href="%(queue)s">%(review)s</a>)'
    ) % {
        "dashboard": dashboard_url,
        "name": contest.name,
        "queue": queue_url,
        "review": "Review",
    }

    if run.triggered_by:
        Notification.objects.create_notification(
            owner=run.triggered_by,
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REVIEW_DONE,
            html_link=author_html_link,
            author=None,
        )

    superuser_profiles = Profile.objects.filter(user__is_superuser=True)
    if run.triggered_by:
        superuser_profiles = superuser_profiles.exclude(id=run.triggered_by.id)
    for profile in superuser_profiles:
        Notification.objects.create_notification(
            owner=profile,
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_NEW,
            html_link=admin_html_link,
            author=run.triggered_by,
        )


def _emit_contest_review_error_notifications(run, reason=""):
    """Notify author + admins that a contest run errored (status=E)."""
    contest = run.contest
    try:
        dashboard_url = reverse("contest_review_dashboard", args=[contest.key])
    except Exception:
        dashboard_url = f"/contest/{contest.key}/review/"

    html_link = '<a href="%(dashboard)s">%(name)s</a>' % {
        "dashboard": dashboard_url,
        "name": contest.name,
    }

    if run.triggered_by:
        Notification.objects.create_notification(
            owner=run.triggered_by,
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REVIEW_ERROR,
            html_link=html_link,
            author=None,
        )

    superuser_profiles = Profile.objects.filter(user__is_superuser=True)
    if run.triggered_by:
        superuser_profiles = superuser_profiles.exclude(id=run.triggered_by.id)
    for profile in superuser_profiles:
        Notification.objects.create_notification(
            owner=profile,
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REVIEW_ERROR,
            html_link=html_link,
            author=run.triggered_by,
        )


@shared_task(bind=True)
def reap_stale_contest_review_runs(self):
    """Mark any ContestReviewRun that has been Running too long as Error.

    Mirrors `reap_stale_review_runs` for problems but uses the contest-specific
    timeout setting and emits CONTEST_* notification categories.
    """
    translation.activate(getattr(settings, "LANGUAGE_CODE", "vi"))
    timeout = getattr(settings, "AUTO_REVIEW_CONTEST_RUN_TIMEOUT_SECONDS", 1800)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=timeout)
    qs = ContestReviewRun.objects.filter(
        status=ContestReviewRun.RUNNING, started_at__lt=cutoff
    )

    stale_runs = list(qs.select_related("contest", "triggered_by"))
    count = qs.update(status=ContestReviewRun.ERROR, finished_at=now)
    for run in stale_runs:
        run.status = ContestReviewRun.ERROR
        run.finished_at = now
        try:
            _emit_contest_review_error_notifications(run, reason="Run timed out")
        except Exception:
            logger.exception(
                "Failed to emit reaper error notifications for contest run %s", run.id
            )
    return {"reaped": count}
