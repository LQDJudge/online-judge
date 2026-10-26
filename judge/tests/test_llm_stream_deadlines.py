"""Offline streaming regressions: no network, provider charges or long sleeps."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

import httpx

from judge.review.coordinator import CoordinatorInfrastructureError, _call
from judge.tests.test_coordinator_pipeline import run_fixture
from llm_service.llm_api import LLMRequestError, LLMRequestTimeout, LLMService
from llm_service.errors import LLMRequestCancelled


class StreamDeadlineTests(SimpleTestCase):
    def service(self, **limits):
        return LLMService(
            api_key="offline-test",
            transport_attempts=1,
            raise_errors=True,
            streaming_timeouts={
                "connect": 0.05,
                "first_output": 0.08,
                "idle": 0.08,
                "total": 0.5,
                **limits,
            },
        )

    def run_stream(self, service, events, failure=None):
        self.closed = False

        async def stream(**kwargs):
            self.transport_kwargs = kwargs
            try:
                for delay, text in events:
                    await asyncio.sleep(delay)
                    yield SimpleNamespace(text=text, is_replace_response=False)
                if failure:
                    raise failure
            finally:
                self.closed = True

        with patch(
            "llm_service.llm_api.fp.stream_request", side_effect=stream
        ), patch.object(service, "_record_usage") as usage, patch(
            "llm_service.llm_api.signal.alarm"
        ) as alarm:
            self.usage = usage
            try:
                return service.call_llm("offline fixture")
            finally:
                alarm.assert_not_called()

    def test_healthy_stream_resets_idle_and_exceeds_first_output_window(self):
        result = self.run_stream(self.service(), [(0.03, "x")] * 5)
        self.assertEqual(result, "xxxxx")
        self.assertTrue(self.closed)
        self.assertEqual(self.transport_kwargs["num_tries"], 1)
        self.assertTrue(self.transport_kwargs["session"].is_closed)
        self.assertEqual(self.transport_kwargs["session"].timeout.connect, 0.05)
        self.assertIsNone(self.transport_kwargs["session"].timeout.read)

    def test_first_output_timeout(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(first_output=0.02), [(0.1, "late")])
        self.assertEqual(caught.exception.kind, "first_output")
        self.assertEqual(caught.exception.raw_response, "")
        self.assertTrue(self.closed)

    def test_idle_timeout_keeps_partial_for_diagnostics_only(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(idle=0.02), [(0, "partial"), (0.1, "late")])
        self.assertEqual(caught.exception.kind, "idle")
        self.assertEqual(caught.exception.raw_response, "partial")
        self.assertEqual(self.usage.call_args.kwargs["response"], "partial")
        self.assertEqual(self.usage.call_args.kwargs["status"], "timeout")
        self.assertTrue(self.closed)

    def test_total_deadline_even_while_stream_is_active(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(total=0.07), [(0.02, "x")] * 10)
        self.assertEqual(caught.exception.kind, "total")
        self.assertTrue(caught.exception.raw_response)

    def test_empty_keepalives_do_not_reset_first_output_deadline(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(first_output=0.04), [(0.01, "")] * 10)
        self.assertEqual(caught.exception.kind, "first_output")

    def test_empty_keepalives_do_not_reset_idle_deadline(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(idle=0.04), [(0, "x")] + [(0.01, "")] * 10)
        self.assertEqual(caught.exception.kind, "idle")

    def test_connection_timeout_is_not_invalid_output(self):
        with self.assertRaises(LLMRequestTimeout) as caught:
            self.run_stream(self.service(), [], httpx.ConnectTimeout("offline"))
        self.assertEqual(caught.exception.kind, "connect")

    def test_transport_failure_is_not_invalid_output(self):
        with self.assertRaises(LLMRequestError) as caught:
            self.run_stream(self.service(), [(0, "partial")], OSError("offline"))
        self.assertEqual(caught.exception.error_category, "provider")
        self.assertEqual(caught.exception.raw_response, "partial")

    def test_cancel_closes_a_stalled_stream_before_timeout(self):
        service = self.service()
        service.should_continue = iter([True, False]).__next__
        service.cancellation_poll_interval = 0.01
        with self.assertRaises(LLMRequestCancelled):
            self.run_stream(service, [(0.2, "late")])
        self.assertTrue(self.closed)
        self.assertTrue(self.transport_kwargs["session"].is_closed)
        self.assertEqual(service.last_error.error_category, "cancelled")

    def test_cancel_before_start_makes_no_provider_request(self):
        service = self.service()
        service.should_continue = lambda: False
        with self.assertRaises(LLMRequestCancelled):
            self.run_stream(service, [(0, "never")])
        self.assertFalse(hasattr(self, "transport_kwargs"))

    def test_cancellation_queries_are_throttled_not_per_chunk(self):
        service = self.service()
        checks = []
        service.should_continue = lambda: checks.append(True) or True
        service.cancellation_poll_interval = 10
        self.assertEqual(self.run_stream(service, [(0, "x")] * 100), "x" * 100)
        self.assertEqual(len(checks), 1)

    def test_opt_in_does_not_change_legacy_failure_contract(self):
        service = self.service(first_output=0.01)
        service.raise_errors = False
        self.assertIsNone(self.run_stream(service, [(0.1, "late")]))

    def test_reject_unbounded_policy_or_hidden_transport_retries(self):
        for value in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.service(total=value)
        with self.assertRaises(ValueError):
            LLMService(
                api_key="offline",
                streaming_timeouts={
                    "connect": 1,
                    "first_output": 1,
                    "idle": 1,
                    "total": 1,
                },
            )

    def test_real_poe_sdk_consumes_sse_and_closes_owned_http_client(self):
        original_client = httpx.AsyncClient
        clients = []
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: text\ndata: {"text":"offline answer"}\n\nevent: done\ndata: {}\n\n',
            )

        def client(**kwargs):
            instance = original_client(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(instance)
            return instance

        with patch(
            "llm_service.llm_api.httpx.AsyncClient", side_effect=client
        ), patch.object(LLMService, "_record_usage"):
            self.assertEqual(self.service().call_llm("fixture"), "offline answer")
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(instance.is_closed for instance in clients))

    def test_real_sdk_wrapped_connection_timeout_is_classified(self):
        original_client = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            raise httpx.ConnectTimeout("offline", request=request)

        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(respond), **kwargs)

        with patch(
            "llm_service.llm_api.httpx.AsyncClient", side_effect=client
        ), patch.object(LLMService, "_record_usage"), self.assertRaises(
            LLMRequestTimeout
        ) as caught:
            self.service().call_llm("fixture")
        self.assertEqual(caught.exception.kind, "connect")
        self.assertEqual(len(requests), 1)

    def test_real_sdk_http_errors_are_classified_before_sse_content_type(self):
        original_client = httpx.AsyncClient
        for status, category in (
            (401, "provider_auth"),
            (402, "provider_quota"),
            (429, "provider_rate_limit"),
            (503, "provider_unavailable"),
        ):
            requests = []

            def respond(request):
                requests.append(request)
                return httpx.Response(
                    status,
                    headers={"Retry-After": "75"},
                    json={"error": "private provider body"},
                )

            def client(**kwargs):
                return original_client(transport=httpx.MockTransport(respond), **kwargs)

            with self.subTest(status=status), patch(
                "llm_service.llm_api.httpx.AsyncClient", side_effect=client
            ), patch.object(LLMService, "_record_usage"), self.assertRaises(
                LLMRequestError
            ) as caught:
                self.service().call_llm("fixture")
            self.assertEqual(caught.exception.error_category, category)
            self.assertEqual(caught.exception.retry_after, 75)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(len(requests), 1)

    def test_cancel_real_sdk_stream_closes_http_response_and_keeps_partial(self):
        original_client = httpx.AsyncClient
        closed = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'event: text\ndata: {"text":"partial"}\n\n'
                await asyncio.sleep(0.2)
                yield b"event: done\ndata: {}\n\n"

            async def aclose(self):
                closed.append(True)

        def respond(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Body()
            )

        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(respond), **kwargs)

        service = self.service()
        service.should_continue = iter([True, False]).__next__
        service.cancellation_poll_interval = 0.01
        with patch(
            "llm_service.llm_api.httpx.AsyncClient", side_effect=client
        ), patch.object(LLMService, "_record_usage"), self.assertRaises(
            LLMRequestCancelled
        ) as caught:
            service.call_llm("fixture")
        self.assertEqual(caught.exception.raw_response, "partial")
        self.assertTrue(closed)

    @patch("judge.review.coordinator.LLMService")
    def test_coordinator_preserves_timeout_category_and_partial(self, provider):
        error = LLMRequestTimeout("idle", 120)
        error.raw_response = '{"unfinished":'
        provider.return_value.call_llm.side_effect = error
        with self.assertRaises(CoordinatorInfrastructureError) as caught:
            _call(run_fixture(), "H1", {"kind": "problem", "available_refs": []})
        self.assertEqual(caught.exception.error_category, "timeout_idle")
        self.assertEqual(caught.exception.raw_response, error.raw_response)
        self.assertEqual(
            caught.exception.usage_json["output_chars"], len(error.raw_response)
        )
        self.assertTrue(provider.call_args.kwargs["raise_errors"])
        self.assertEqual(provider.call_args.kwargs["timeout"], 480)

    @override_settings(REVIEW_LLM_TIMEOUT=600)
    @patch("judge.review.coordinator.LLMService")
    def test_bad_lease_alignment_fails_before_any_provider_call(self, provider):
        with self.assertRaises(CoordinatorInfrastructureError) as caught:
            _call(run_fixture(), "H1", {"kind": "problem", "available_refs": []})
        self.assertEqual(caught.exception.error_category, "configuration")
        provider.assert_not_called()
