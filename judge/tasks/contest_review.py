"""Stable contest task names backed by the common phase engine."""

from celery import shared_task

from judge.models import ContestReviewRun
from judge.auto_review.scheduling import schedule_phases
from judge.tasks.coordinator import reap_review_runs


@shared_task
def review_contest(run_id):
    """Resume an admitted run; child creation and validation belong to admission."""
    run = ContestReviewRun.objects.filter(pk=run_id, status="R").first()
    if run is None or not run.coordinator_stages.exists():
        return {"success": False, "error": "No active admitted review."}
    schedule_phases("contest", run_id)
    return {"success": True, "run_id": run_id}


@shared_task
def reap_stale_contest_review_runs():
    return reap_review_runs("contest")
