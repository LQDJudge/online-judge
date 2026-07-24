import json
import logging
import re

from celery import shared_task

from judge.models import UsernameModerationCase
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


def parse_username_moderation_response(response):
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
        UsernameModerationCase.DECISION_ALLOW,
        UsernameModerationCase.DECISION_REVIEW,
        UsernameModerationCase.DECISION_BLOCK,
    }:
        return None
    if category not in dict(UsernameModerationCase.CATEGORY_CHOICES):
        category = UsernameModerationCase.CATEGORY_OTHER

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


@shared_task(bind=True)
def moderate_username_task(self, case_id, delete_safe_case=False):
    case = UsernameModerationCase.objects.select_related("user").get(id=case_id)
    if case.status == UsernameModerationCase.STATUS_REVIEWED:
        return {"skipped": True, "reason": "already reviewed"}

    prompt = "Username to classify:\n%s" % case.username

    try:
        config = get_config()
        llm = LLMService(
            api_key=config.api_key,
            bot_name=config.get_bot_name_for_moderation(),
            sleep_time=config.sleep_time,
            timeout=min(config.timeout, 60),
        )
        response = llm.call_llm(prompt, system_prompt=USERNAME_MODERATION_SYSTEM_PROMPT)
        result = parse_username_moderation_response(response)
    except Exception as exc:
        logger.exception("Username moderation failed for case %s", case_id)
        case.decision = UsernameModerationCase.DECISION_REVIEW
        case.reason = "AI moderation failed: %s" % exc
        case.is_automated = True
        case.save(update_fields=["decision", "reason", "is_automated", "updated_at"])
        return {"error": str(exc), "decision": case.decision}

    if result is None:
        case.decision = UsernameModerationCase.DECISION_REVIEW
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

    if result["decision"] == UsernameModerationCase.DECISION_BLOCK:
        case.disable_user(hide_identity=True)
    elif result["decision"] == UsernameModerationCase.DECISION_ALLOW:
        if delete_safe_case and case.source == UsernameModerationCase.SOURCE_AUDIT:
            case.delete()
            return {
                "decision": result["decision"],
                "category": result["category"],
                "confidence": result["confidence"],
                "status": "deleted",
            }
        case.allow()
    else:
        case.status = UsernameModerationCase.STATUS_PENDING
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
