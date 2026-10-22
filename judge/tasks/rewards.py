from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command


@shared_task
def award_course_frames():
    if not getattr(settings, "COURSE_REWARDS_ENABLED", True):
        return {"skipped": True, "reason": "disabled"}
    return run_locked_command(
        "periodic:award_course_frames",
        "sync_course_rewards",
        "--batch-size",
        str(getattr(settings, "COURSE_REWARDS_BATCH_SIZE", 100)),
        lock_timeout=3600,
    )
