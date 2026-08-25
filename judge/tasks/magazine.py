import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from judge.models import BlogPost, Organization

logger = logging.getLogger(__name__)


@shared_task
def generate_daily_magazine_posts():
    if not getattr(settings, "PERIODIC_GENERATE_MAGAZINE_POSTS_ENABLED", True):
        logger.info("Daily magazine generation skipped because it is disabled")
        return {"skipped": True, "reason": "disabled"}

    lock_key = "periodic:generate_daily_magazine_posts"
    lock_timeout = getattr(
        settings, "PERIODIC_GENERATE_MAGAZINE_POSTS_LOCK_TIMEOUT", 12 * 3600
    )
    token = uuid.uuid4().hex
    if not cache.add(lock_key, token, lock_timeout):
        logger.info("Daily magazine generation skipped because lock is held")
        return {"skipped": True, "reason": "locked"}

    try:
        return _generate_daily_magazine_posts()
    finally:
        if cache.get(lock_key) == token:
            cache.delete(lock_key)


def _generate_daily_magazine_posts():
    configured_slugs = tuple(getattr(settings, "MAGAZINE_COMMUNITY_ORG_SLUGS", ()))
    excluded_slugs = tuple(
        getattr(
            settings,
            "MAGAZINE_EXCLUDED_COMMUNITY_ORG_SLUGS",
            ("hoi-dap-thac-mac", "off-topic"),
        )
    )
    per_org_count = int(getattr(settings, "MAGAZINE_POSTS_PER_COMMUNITY_PER_RUN", 1))
    max_pending = int(getattr(settings, "MAGAZINE_MAX_PENDING_PER_COMMUNITY", 5))
    author = getattr(settings, "MAGAZINE_AUTHOR_USERNAME", "admin")
    post_type = getattr(settings, "MAGAZINE_POST_TYPE", "mixed")
    llm = getattr(settings, "MAGAZINE_LLM_BOT", "Gemini-3.7-Flash")
    max_attempts = int(getattr(settings, "MAGAZINE_MAX_ATTEMPTS", 12))
    candidate_drafts = int(getattr(settings, "MAGAZINE_CANDIDATE_DRAFTS", 1))
    review_threshold = int(getattr(settings, "MAGAZINE_REVIEW_THRESHOLD", 9))

    organizations = Organization.objects.filter(is_community=True)
    if configured_slugs:
        organizations = organizations.filter(slug__in=configured_slugs)
    if excluded_slugs:
        organizations = organizations.exclude(slug__in=excluded_slugs)

    today = timezone.localdate().isoformat()
    generated = []
    skipped = []
    failed = []

    for org in organizations.order_by("slug"):
        pending_count = (
            BlogPost.objects.filter(
                organizations=org,
                visible=False,
                is_rejected=False,
            )
            .distinct()
            .count()
        )
        if pending_count >= max_pending:
            skipped.append(
                {
                    "org": org.slug,
                    "reason": "pending_limit",
                    "pending_count": pending_count,
                }
            )
            continue

        seed = "daily-magazine-%s-%s" % (today, org.slug)
        try:
            call_command(
                "generate_magazine_posts",
                org=org.slug,
                author=author,
                count=per_org_count,
                seed=seed,
                difficulty="random",
                post_type=post_type,
                problem=None,
                contest=None,
                topic=None,
                update_post_id=None,
                evaluate_post_id=None,
                commit=True,
                publish=False,
                llm=llm,
                max_attempts=max_attempts,
                skip_review=False,
                review_threshold=review_threshold,
                candidate_drafts=candidate_drafts,
            )
            generated.append(org.slug)
        except Exception as exc:
            logger.exception("Failed to generate magazine post for %s", org.slug)
            failed.append({"org": org.slug, "error": str(exc)})

    return {
        "success": not failed,
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
    }
