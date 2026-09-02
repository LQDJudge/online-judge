import json
import logging
import re

from celery import shared_task
from django.utils.timezone import now

from judge.models import ProfileModerationCase
from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


USERNAME_MODERATION_SYSTEM_PROMPT = """
You are a strict username moderation classifier for an educational programming site.
Classify usernames in any language, including Vietnamese.

Block usernames that are gambling/casino/betting spam, obscene, sexually explicit,
offensive, hateful, targeted harassment, impersonation, deceptive site-brand abuse,
or other harmful policy violations.

Jokes are allowed when they are not harmful, obscene, deceptive, or abusive.

Return exactly one JSON object with:
{
  "decision": "allow" | "review" | "block",
  "category": "safe" | "gambling" | "offensive" | "obscene" | "impersonation" | "spam" | "other",
  "confidence": 0.0-1.0,
  "reason": "short explanation"
}
""".strip()


PROFILE_ABOUT_MODERATION_SYSTEM_PROMPT = """
You are moderating a public self-description on an educational programming site.

Default to allow. Users may share normal personal introductions, real names,
school/class information, achievements, programming interests, GitHub/social
links, harmless jokes, and harmless self-promotion.

Flag only clearly unsafe public content:
- gambling, casino, betting, or adult-service spam
- phishing, malware, scam links, fake login/support links, or credential requests
- obscene or sexually explicit content
- hateful, threatening, or targeted harassment
- unrelated commercial spam or mass promotion
- harmful impersonation, such as claiming to be site admin/staff/support or
  speaking as the official site/team

Do not block normal personal information, school information, social links,
portfolio links, programming achievements, or ordinary self-expression.

Return exactly one JSON object with:
{
  "decision": "allow" | "review" | "block",
  "category": "safe" | "gambling" | "offensive" | "obscene" | "impersonation" | "spam" | "other",
  "confidence": 0.0-1.0,
  "reason": "short explanation"
}
""".strip()


def parse_profile_moderation_response(response):
    if not response:
        return None
    match = re.search(r"\{.*\}", response, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    decision = str(data.get("decision", "")).lower()
    category = str(data.get("category", "")).lower()
    if decision not in {
        ProfileModerationCase.DECISION_ALLOW,
        ProfileModerationCase.DECISION_REVIEW,
        ProfileModerationCase.DECISION_BLOCK,
    }:
        return None
    if category not in dict(ProfileModerationCase.CATEGORY_CHOICES):
        category = ProfileModerationCase.CATEGORY_OTHER

    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(confidence, 1))

    return {
        "decision": decision,
        "category": category,
        "confidence": confidence,
        "reason": str(data.get("reason", ""))[:1000],
        "raw_response": data,
    }


parse_username_moderation_response = parse_profile_moderation_response


def get_profile_moderation_prompt(case):
    if case.target == ProfileModerationCase.TARGET_ABOUT:
        return (
            "Profile self-description to classify:\n%s" % case.display_value,
            PROFILE_ABOUT_MODERATION_SYSTEM_PROMPT,
        )
    return (
        "Username to classify:\n%s" % case.display_value,
        USERNAME_MODERATION_SYSTEM_PROMPT,
    )


def mark_stale_profile_moderation_case(case):
    case.status = ProfileModerationCase.STATUS_REVIEWED
    case.decision = ProfileModerationCase.DECISION_ALLOW
    case.category = ProfileModerationCase.CATEGORY_SAFE
    case.reason = (
        "Skipped because the profile self-description changed before moderation "
        "completed."
    )
    case.is_automated = True
    case.decided_at = now()
    case.save(
        update_fields=[
            "status",
            "decision",
            "category",
            "reason",
            "is_automated",
            "decided_at",
            "updated_at",
        ]
    )


@shared_task(bind=True)
def moderate_profile_case_task(
    self, case_id, delete_safe_case=False, trigger_user_id=None
):
    case = ProfileModerationCase.objects.select_related("user", "user__profile").get(
        id=case_id
    )
    if case.status == ProfileModerationCase.STATUS_REVIEWED:
        return {"skipped": True, "reason": "already reviewed"}

    if case.target == ProfileModerationCase.TARGET_ABOUT and (
        case.user.profile.about or ""
    ) != (case.value_snapshot or ""):
        mark_stale_profile_moderation_case(case)
        return {"skipped": True, "reason": "stale profile self-description"}

    prompt, system_prompt = get_profile_moderation_prompt(case)
    feature = (
        "profile_moderation"
        if case.target == ProfileModerationCase.TARGET_ABOUT
        else "username_moderation"
    )

    try:
        config = get_config()
        llm = LLMService(
            api_key=config.api_key,
            bot_name=config.get_bot_name_for_moderation(),
            sleep_time=config.sleep_time,
            timeout=min(config.timeout, 60),
            feature=feature,
            user_id=trigger_user_id,
            parameters=config.get_parameters_for_moderation(),
            metadata={
                "case_id": case.id,
                "target": case.target,
                "source": case.source,
                "target_user_id": case.user_id,
                "target_username": case.username,
            },
        )
        response = llm.call_llm(prompt, system_prompt=system_prompt)
        result = parse_profile_moderation_response(response)
    except Exception as exc:
        logger.exception("Profile moderation failed for case %s", case_id)
        case.decision = ProfileModerationCase.DECISION_REVIEW
        case.reason = "AI profile moderation failed: %s" % exc
        case.is_automated = True
        case.save(update_fields=["decision", "reason", "is_automated", "updated_at"])
        return {"error": str(exc), "decision": case.decision}

    if result is None:
        case.decision = ProfileModerationCase.DECISION_REVIEW
        case.reason = "AI moderation returned an unparsable response."
        case.raw_response = {"response": response}
        case.is_automated = True
        case.save(
            update_fields=[
                "decision",
                "reason",
                "raw_response",
                "is_automated",
                "updated_at",
            ]
        )
        return {"error": "unparsable", "decision": case.decision}

    case.decision = result["decision"]
    case.category = result["category"]
    case.confidence = result["confidence"]
    case.reason = result["reason"]
    case.raw_response = result["raw_response"]
    case.is_automated = True

    if result["decision"] == ProfileModerationCase.DECISION_BLOCK:
        if case.target == ProfileModerationCase.TARGET_USERNAME:
            case.disable_user(hide_identity=True)
        else:
            case.status = ProfileModerationCase.STATUS_REVIEWED
            case.public_identity_hidden = True
            case.decided_at = now()
            case.save(
                update_fields=[
                    "status",
                    "decision",
                    "category",
                    "confidence",
                    "reason",
                    "raw_response",
                    "public_identity_hidden",
                    "is_automated",
                    "decided_at",
                    "updated_at",
                ]
            )
    elif result["decision"] == ProfileModerationCase.DECISION_ALLOW:
        if delete_safe_case and case.source == ProfileModerationCase.SOURCE_AUDIT:
            case.delete()
            return {
                "decision": result["decision"],
                "category": result["category"],
                "confidence": result["confidence"],
                "status": "deleted",
            }
        case.allow()
    else:
        case.status = ProfileModerationCase.STATUS_PENDING
        case.save(
            update_fields=[
                "decision",
                "category",
                "confidence",
                "reason",
                "raw_response",
                "is_automated",
                "updated_at",
            ]
        )

    return {
        "decision": case.decision,
        "category": case.category,
        "confidence": case.confidence,
        "status": case.status,
    }


@shared_task(bind=True)
def moderate_username_task(self, case_id, delete_safe_case=False, trigger_user_id=None):
    return moderate_profile_case_task(
        case_id, delete_safe_case=delete_safe_case, trigger_user_id=trigger_user_id
    )
