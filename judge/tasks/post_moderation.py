import logging

from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command

logger = logging.getLogger(__name__)


@shared_task
def moderate_pending_posts():
    if not getattr(settings, "AUTO_MODERATE_POSTS_ENABLED", True):
        logger.info("Post moderation task skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:auto_moderate_posts",
        "auto_moderate",
        "--posts-only",
        "--batch-size",
        str(getattr(settings, "AUTO_MODERATE_POSTS_BATCH_SIZE", 50)),
        lock_timeout=getattr(settings, "AUTO_MODERATE_POSTS_LOCK_TIMEOUT", 1800),
    )
