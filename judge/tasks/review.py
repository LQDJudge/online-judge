"""Stable task names for the single shared review workflow."""

from celery import shared_task

from judge.models import ProblemReviewRun
from judge.auto_review.scheduling import schedule_phases
from judge.tasks.coordinator import reap_review_runs


@shared_task
def review_problem(run_id, emit_notifications=True):
    """Resume an admitted run; never revive historical reports or bypass admission."""
    run = ProblemReviewRun.objects.filter(pk=run_id, status="R").first()
    if run is None or not run.coordinator_stages.exists():
        return {"success": False, "error": "No active admitted review."}
    schedule_phases("problem", run_id)
    return {"success": True, "run_id": run_id}


@shared_task
def reap_stale_review_runs():
    """Reuse the periodic task name without keeping a second timeout mechanism."""
    return reap_review_runs("problem")
