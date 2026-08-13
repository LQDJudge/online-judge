import logging

from celery import shared_task
from django.conf import settings

from judge.tasks.periodic import run_locked_command

logger = logging.getLogger(__name__)


@shared_task
def cleanup_inactive_accounts():
    if not getattr(settings, "PERIODIC_CLEANUP_INACTIVE_ENABLED", True):
        logger.info("Inactive account cleanup skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:cleanup_inactive_accounts",
        "cleanup_inactive",
        "--users",
        "--orgs",
        "--batch-size",
        str(getattr(settings, "PERIODIC_CLEANUP_INACTIVE_BATCH_SIZE", 100)),
        lock_timeout=getattr(settings, "PERIODIC_CLEANUP_INACTIVE_LOCK_TIMEOUT", 3600),
    )


@shared_task
def delete_old_notifications():
    if not getattr(settings, "PERIODIC_DELETE_OLD_NOTIFICATIONS_ENABLED", True):
        logger.info("Old notification cleanup skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:delete_old_notifications",
        "delete_old_notifications",
        "--batch-size",
        str(getattr(settings, "PERIODIC_DELETE_OLD_NOTIFICATIONS_BATCH_SIZE", 1000)),
        lock_timeout=getattr(
            settings, "PERIODIC_DELETE_OLD_NOTIFICATIONS_LOCK_TIMEOUT", 3600
        ),
    )


@shared_task
def delete_old_request_metrics():
    if not getattr(settings, "PERIODIC_DELETE_OLD_REQUEST_METRICS_ENABLED", True):
        logger.info("Request metric cleanup skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:delete_old_request_metrics",
        "delete_old_request_metrics",
        "--days",
        str(getattr(settings, "REQUEST_METRICS_RETENTION_DAYS", 7)),
        "--batch-size",
        str(getattr(settings, "PERIODIC_DELETE_OLD_REQUEST_METRICS_BATCH_SIZE", 1000)),
        lock_timeout=getattr(
            settings, "PERIODIC_DELETE_OLD_REQUEST_METRICS_LOCK_TIMEOUT", 3600
        ),
    )


@shared_task
def clear_expired_sessions():
    if not getattr(settings, "PERIODIC_CLEAR_EXPIRED_SESSIONS_ENABLED", True):
        logger.info("Expired session cleanup skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:clear_expired_sessions",
        "batch_clearsessions",
        "--batch-size",
        str(getattr(settings, "PERIODIC_CLEAR_EXPIRED_SESSIONS_BATCH_SIZE", 1000)),
        "--sleep",
        str(getattr(settings, "PERIODIC_CLEAR_EXPIRED_SESSIONS_SLEEP", 0.5)),
        lock_timeout=getattr(
            settings, "PERIODIC_CLEAR_EXPIRED_SESSIONS_LOCK_TIMEOUT", 3600
        ),
    )


@shared_task
def recompute_comment_scores():
    if not getattr(settings, "PERIODIC_RECOMPUTE_COMMENT_SCORES_ENABLED", True):
        logger.info("Comment score recompute skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:recompute_comment_scores",
        "recompute_comment_scores",
        lock_timeout=getattr(
            settings, "PERIODIC_RECOMPUTE_COMMENT_SCORES_LOCK_TIMEOUT", 3600
        ),
    )


@shared_task
def recompute_contributions():
    if not getattr(settings, "PERIODIC_RECOMPUTE_CONTRIBUTIONS_ENABLED", True):
        logger.info("Contribution recompute skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:recompute_contributions",
        "recompute_contributions",
        lock_timeout=getattr(
            settings, "PERIODIC_RECOMPUTE_CONTRIBUTIONS_LOCK_TIMEOUT", 7200
        ),
    )


@shared_task
def sync_organization_private_flags():
    if not getattr(settings, "PERIODIC_FIX_ORGANIZATION_PRIVATE_ENABLED", True):
        logger.info("Organization private flag sync skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    return run_locked_command(
        "periodic:fix_organization_private",
        "fix_organization_private",
        lock_timeout=getattr(
            settings, "PERIODIC_FIX_ORGANIZATION_PRIVATE_LOCK_TIMEOUT", 3600
        ),
    )
