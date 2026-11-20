from celery import shared_task

from judge.tasks.omni_moderation import recover_omni_items
from judge.tasks.periodic import run_locked_command


@shared_task
def moderate_recent_chat():
    recovery = recover_omni_items("chat")
    muse = run_locked_command(
        "periodic:auto_moderate_chat", "auto_moderate", "--chat-only", lock_timeout=900
    )
    return {"recovery": recovery, "muse": muse}
