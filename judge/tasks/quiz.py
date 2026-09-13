from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command


@shared_task
def expire_quiz_attempts():
    if not getattr(settings, "QUIZ_EXPIRY_ENABLED", False):
        return {"skipped": True, "reason": "disabled"}
    return run_locked_command(
        "periodic:expire-quiz-attempts",
        "expire_quiz_attempts",
        "--batch-size",
        str(getattr(settings, "QUIZ_EXPIRY_BATCH_SIZE", 100)),
        "--max-seconds",
        "20",
        lock_timeout=60,
    )
