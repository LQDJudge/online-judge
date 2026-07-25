import logging

from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task
def moderate_recent_comments():
    if not getattr(settings, "AUTO_MODERATE_COMMENTS_ENABLED", True):
        logger.info("Comment moderation task skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    call_command(
        "auto_moderate",
        "--comments-only",
        "--batch-size",
        str(getattr(settings, "AUTO_MODERATE_COMMENTS_BATCH_SIZE", 50)),
        "--comment-window-minutes",
        str(getattr(settings, "AUTO_MODERATE_COMMENTS_WINDOW_MINUTES", 60)),
    )
    return {"success": True}
