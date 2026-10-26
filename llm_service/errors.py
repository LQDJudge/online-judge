"""Safe transport classifications; never expose provider bodies or credentials."""

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from fastapi_poe.client import BotErrorNoRetry


class LLMRequestError(Exception):
    error_category = "provider"
    retryable = True
    retry_after = None


class LLMRequestTimeout(LLMRequestError):
    def __init__(self, kind, seconds):
        self.kind = kind
        self.seconds = seconds
        self.error_category = "timeout_" + kind
        super().__init__("AI request %s timeout after %ss" % (kind, seconds))


class LLMRequestCancelled(LLMRequestError):
    error_category = "cancelled"
    retryable = False


def retry_after_seconds(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def classify_provider_error(exc):
    if isinstance(exc, LLMRequestError):
        return exc
    causes = []
    cause = exc
    for _ in range(8):
        causes.append(cause)
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    # Inspect structured HTTP errors even when wrapped in Poe's BotError.
    for cause in causes:
        if not isinstance(cause, httpx.HTTPStatusError):
            continue
        status = cause.response.status_code
        if status in (401, 403):
            category, retryable = "provider_auth", False
        elif status == 402:
            category, retryable = "provider_quota", False
        elif status == 429:
            category, retryable = "provider_rate_limit", True
        elif status in (408, 425) or status >= 500:
            category, retryable = "provider_unavailable", True
        elif 400 <= status < 500:
            category, retryable = "provider_request", False
        else:
            category, retryable = "provider", True
        error = LLMRequestError("AI provider request failed (%s)" % category)
        error.error_category = category
        error.retryable = retryable
        error.retry_after = retry_after_seconds(
            cause.response.headers.get("Retry-After")
        )
        return error
    error = LLMRequestError("The AI provider request could not complete")
    if any(isinstance(cause, BotErrorNoRetry) for cause in causes):
        error.error_category = "provider_rejected"
        error.retryable = False
    return error
