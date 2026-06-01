"""Thin LLM helper for review checks: JSON response + retries."""

import json
import logging
import re

from django.conf import settings

from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


class LLMCallFailed(Exception):
    pass


def call_llm_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Call the LLM with the given prompts, expecting a JSON object back.
    Retries up to AUTO_REVIEW_LLM_RETRY_COUNT times. Strips ```json fences.
    Raises LLMCallFailed if every attempt fails or returns unparseable JSON.
    """
    retries = getattr(settings, "AUTO_REVIEW_LLM_RETRY_COUNT", 3)
    config = get_config()
    service = LLMService(
        api_key=config.api_key, bot_name=config.get_bot_name_for_review()
    )

    last_exc = None
    for attempt in range(retries):
        try:
            response = service.call_llm(user_prompt, system_prompt=system_prompt)
        except Exception as exc:
            logger.warning(
                "LLM call attempt %d/%d failed: %s", attempt + 1, retries, exc
            )
            last_exc = exc
            continue

        if not response:
            last_exc = LLMCallFailed("Empty response")
            continue

        text = response.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "LLM returned non-JSON (attempt %d/%d): %r",
                attempt + 1,
                retries,
                text[:200],
            )
            last_exc = exc

    raise LLMCallFailed(f"All {retries} attempts failed: {last_exc}")
