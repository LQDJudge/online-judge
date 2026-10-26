from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

import httpx
from fastapi_poe.client import BotError, BotErrorNoRetry

from ai_features.markdown_improver import MarkdownImprover
from ai_features.problem_tagger import ProblemTagger
from ai_features.quiz_ai_service import QuizAIService
from ai_features.solution_code_generator import SolutionCodeGenerator
from ai_features.solution_generator import SolutionGenerator
from llm_service.errors import classify_provider_error, retry_after_seconds
from llm_service.llm_api import LLMService
from llm_service.policies import FEATURE_TIMEOUTS, streaming_options


def http_failure(status, retry_after=None):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://offline.invalid"),
        headers={"Retry-After": retry_after} if retry_after else {},
    )
    cause = httpx.HTTPStatusError(
        "private provider body or token", request=response.request, response=response
    )
    wrapper = BotError("SDK wrapper")
    wrapper.__cause__ = cause
    return wrapper


class ProviderClassificationTests(SimpleTestCase):
    def test_wrapped_http_errors_have_safe_categories_and_retry_policies(self):
        cases = [
            (401, "provider_auth", False),
            (403, "provider_auth", False),
            (402, "provider_quota", False),
            (404, "provider_request", False),
            (422, "provider_request", False),
            (429, "provider_rate_limit", True),
            (503, "provider_unavailable", True),
        ]
        for status, category, retryable in cases:
            with self.subTest(status=status):
                error = classify_provider_error(http_failure(status, "75"))
                self.assertEqual(error.error_category, category)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.retry_after, 75)
                self.assertNotIn("private", str(error))

    def test_provider_no_retry_is_respected(self):
        error = classify_provider_error(BotErrorNoRetry("private"))
        self.assertFalse(error.retryable)
        self.assertEqual(error.error_category, "provider_rejected")

    def test_retry_after_supports_http_date_and_rejects_invalid_numbers(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
        self.assertGreater(retry_after_seconds(future), 88)
        self.assertLessEqual(retry_after_seconds(future), 90)
        for value in ("nan", "inf", "not a date", None):
            self.assertIsNone(retry_after_seconds(value))
        self.assertEqual(retry_after_seconds("-5"), 0)

    def test_inline_retries_do_not_sleep_through_long_rate_limits(self):
        service = LLMService(api_key="offline")
        service.last_error = classify_provider_error(http_failure(429, "75"))
        self.assertIsNone(service.retry_delay())
        service.last_error = classify_provider_error(http_failure(429, "12"))
        self.assertEqual(service.retry_delay(), 12)
        service.last_error = classify_provider_error(http_failure(401))
        self.assertIsNone(service.retry_delay())

    def test_classification_survives_llm_service_and_retains_partial(self):
        service = LLMService(api_key="offline", raise_errors=True)
        with patch(
            "llm_service.llm_api.fp.get_bot_response_sync",
            side_effect=http_failure(429, "45"),
        ), patch.object(service, "_record_usage"), self.assertRaises(
            Exception
        ) as caught:
            service.call_llm("fixture")
        self.assertEqual(caught.exception.error_category, "provider_rate_limit")
        self.assertEqual(caught.exception.retry_after, 45)


class ConsumerPolicyTests(SimpleTestCase):
    def test_each_policy_fits_worker_budget_and_has_one_transport_attempt(self):
        for feature in FEATURE_TIMEOUTS:
            options = streaming_options(feature)
            self.assertLessEqual(options["timeout"] + 60, 600)
            self.assertEqual(options["transport_attempts"], 1)
            service = LLMService(api_key="offline", **options)
            self.assertFalse(service.raise_errors)

    @override_settings(AI_FEATURE_STREAMING_TIMEOUTS={"quiz_import": {"total": 900}})
    def test_bad_consumer_override_is_rejected(self):
        with self.assertRaises(ValueError):
            streaming_options("quiz_import")

    def test_existing_generators_opt_into_their_own_policy(self):
        constructors = [
            (MarkdownImprover, "problem_markdown"),
            (ProblemTagger, "problem_tagging"),
            (SolutionGenerator, "solution_generation"),
            (SolutionCodeGenerator, "solution_code_generation"),
        ]
        for constructor, feature in constructors:
            with self.subTest(feature=feature):
                service = constructor(api_key="offline").llm_service
                self.assertEqual(
                    service.streaming_timeouts,
                    streaming_options(feature)["streaming_timeouts"],
                )
                self.assertEqual(service.transport_attempts, 1)
        with patch("ai_features.quiz_ai_service.get_config") as config:
            config.return_value.api_key = "offline"
            service = QuizAIService().llm_service
        self.assertEqual(
            service.streaming_timeouts,
            streaming_options("quiz_ai")["streaming_timeouts"],
        )
