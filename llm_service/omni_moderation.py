"""Small client for the OpenAI moderation endpoint.

This module only returns classifications; callers decide what action to take.
"""

import math
import os

import requests
from django.conf import settings


class OmniModerationError(Exception):
    """A moderation request failed or returned an invalid result."""

    def __init__(self, message, *, category="unknown", status_code=None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code


def moderate_content(text, image_urls=()):
    key = getattr(settings, "OPENAI_API_KEY", None) or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise OmniModerationError("OpenAI moderation API key is not configured")
    content = [{"type": "text", "text": text}]
    content.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in image_urls
    )
    try:
        response = requests.post(
            "https://api.openai.com/v1/moderations",
            headers={"Authorization": "Bearer " + key},
            json={"model": "omni-moderation-latest", "input": content},
            timeout=(5, 30),
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["results"][0]
        categories = result["categories"]
        scores = result["category_scores"]
        if (
            not isinstance(result["flagged"], bool)
            or not isinstance(categories, dict)
            or not categories
            or any(not isinstance(value, bool) for value in categories.values())
            or not isinstance(scores, dict)
            or set(scores) != set(categories)
            or any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
                for value in scores.values()
            )
        ):
            raise ValueError("Invalid moderation response")
        return result
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status_code = response.status_code if response is not None else None
        category = "http_error" if status_code else "transport_error"
        raise OmniModerationError(
            "OpenAI moderation request failed",
            category=category,
            status_code=status_code,
        ) from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # Avoid recording response bodies, credentials, or user-provided content.
        raise OmniModerationError(
            "OpenAI moderation request failed", category="invalid_response"
        ) from exc
