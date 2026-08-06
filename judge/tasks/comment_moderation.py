import logging

from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command

logger = logging.getLogger(__name__)


@shared_task
def moderate_recent_comments():
    if not getattr(settings, "AUTO_MODERATE_COMMENTS_ENABLED", True):
        logger.info("Comment moderation task skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:auto_moderate_comments",
        "auto_moderate",
        "--comments-only",
        "--batch-size",
        str(getattr(settings, "AUTO_MODERATE_COMMENTS_BATCH_SIZE", 50)),
        "--comment-window-minutes",
        str(getattr(settings, "AUTO_MODERATE_COMMENTS_WINDOW_MINUTES", 120)),
        lock_timeout=getattr(settings, "AUTO_MODERATE_COMMENTS_LOCK_TIMEOUT", 900),
    )
