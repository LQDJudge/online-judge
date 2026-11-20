from celery import shared_task

from judge.tasks.omni_moderation import recover_omni_items
from judge.tasks.periodic import run_locked_command


@shared_task
def moderate_recent_comments():
    recovery = recover_omni_items("comment")
    muse = run_locked_command(
        "periodic:auto_moderate_comments",
        "auto_moderate",
        "--comments-only",
        lock_timeout=900,
    )
    return {"recovery": recovery, "muse": muse}
