import logging

from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command

logger = logging.getLogger(__name__)


@shared_task
def moderate_recent_chat():
    if not getattr(settings, "AUTO_MODERATE_CHAT_ENABLED", True):
        logger.info("Chat moderation task skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:auto_moderate_chat",
        "auto_moderate",
        "--chat-only",
        "--batch-size",
        str(getattr(settings, "AUTO_MODERATE_CHAT_BATCH_SIZE", 100)),
        "--chat-window-minutes",
        str(getattr(settings, "AUTO_MODERATE_CHAT_WINDOW_MINUTES", 120)),
        lock_timeout=getattr(settings, "AUTO_MODERATE_CHAT_LOCK_TIMEOUT", 900),
    )
