"""
Tests for the built-in rate limiting system
"""

import time
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse

from judge.utils.ratelimit import (
    parse_rate,
    get_cache_key,
    get_client_ip,
    is_rate_limited,
    create_rate_limit_response,
    ratelimit,
    RateLimitExceeded,
)


class ParseRateTestCase(TestCase):
    """Test rate parsing functionality"""

    def test_parse_valid_rates(self):
        """Test parsing of valid rate strings"""
        test_cases = [
            ("30/h", (30, 3600)),
            ("200/h", (200, 3600)),
            ("10/m", (10, 60)),
            ("5/s", (5, 1)),
            ("1/d", (1, 86400)),
            ("100/H", (100, 3600)),  # Case insensitive
        ]

        for rate_str, expected in test_cases:
            with self.subTest(rate=rate_str):
                result = parse_rate(rate_str)
                self.assertEqual(result, expected)

    def test_parse_invalid_rates(self):
        """Test parsing of invalid rate strings"""
        invalid_rates = [
            "",
            "30",
            "30/",
            "/h",
            "30/x",
            "abc/h",
            "30/hour",
            "30-h",
        ]

        for rate_str in invalid_rates:
            with self.subTest(rate=rate_str):
                with self.assertRaises(ValueError):
                    parse_rate(rate_str)


class GetClientIpTestCase(TestCase):
    """Test client IP detection"""

    def setUp(self):
        self.factory = RequestFactory()

    def test_x_forwarded_for(self):
        """Test IP detection from X-Forwarded-For header"""
        request = self.factory.get("/")
        request.META["HTTP_X_FORWARDED_FOR"] = "192.168.1.1, 10.0.0.1"

        ip = get_client_ip(request)
        self.assertEqual(ip, "192.168.1.1")

    def test_x_real_ip(self):
        """Test IP detection from X-Real-IP header"""
        request = self.factory.get("/")
        request.META["HTTP_X_REAL_IP"] = "192.168.1.2"

        ip = get_client_ip(request)
        self.assertEqual(ip, "192.168.1.2")

    def test_remote_addr(self):
        """Test IP detection from REMOTE_ADDR"""
        request = self.factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.3"

        ip = get_client_ip(request)
        self.assertEqual(ip, "192.168.1.3")

    def test_no_ip_fallback(self):
        """Test fallback when no IP is available"""
        request = self.factory.get("/")
        # Clear all IP-related headers
        if "REMOTE_ADDR" in request.META:
            del request.META["REMOTE_ADDR"]

        ip = get_client_ip(request)
        self.assertEqual(ip, "unknown")


class GetCacheKeyTestCase(TestCase):
    """Test cache key generation"""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass"
        )

    def test_user_key_authenticated(self):
        """Test cache key for authenticated user"""
        request = self.factory.get("/")
        request.user = self.user

        key = get_cache_key(request, "user", "test_view")
        expected = f"ratelimit:user:{self.user.id}:test_view"
        self.assertEqual(key, expected)

    def test_user_key_anonymous(self):
        """Test cache key for anonymous user falls back to IP"""
        request = self.factory.get("/")
        request.user = MagicMock()
        request.user.is_authenticated = False
        request.META["REMOTE_ADDR"] = "192.168.1.1"

        key = get_cache_key(request, "user", "test_view")
        expected = "ratelimit:user:192.168.1.1:test_view"
        self.assertEqual(key, expected)

    def test_ip_key(self):
        """Test cache key for IP-based rate limiting"""
        request = self.factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.1"

        key = get_cache_key(request, "ip", "test_view")
        expected = "ratelimit:ip:192.168.1.1:test_view"
        self.assertEqual(key, expected)

    def test_header_key(self):
        """Test cache key for header-based rate limiting"""
        request = self.factory.get("/")
        request.META["HTTP_X_API_KEY"] = "test-api-key"

        key = get_cache_key(request, "header:x-api-key", "test_view")
        expected = "ratelimit:header:x-api-key:test-api-key:test_view"
        self.assertEqual(key, expected)


class IsRateLimitedTestCase(TestCase):
    """Test rate limiting logic"""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_first_request_not_limited(self):
        """Test that first request is not rate limited"""
        is_limited, count, reset_time = is_rate_limited("test_key", 5, 3600)

        self.assertFalse(is_limited)
        self.assertEqual(count, 1)
        self.assertGreater(reset_time, time.time())

    def test_within_limit_not_limited(self):
        """Test requests within limit are not blocked"""
        cache_key = "test_key_within"

        # Make 3 requests (limit is 5)
        for i in range(3):
            is_limited, count, reset_time = is_rate_limited(cache_key, 5, 3600)
            self.assertFalse(is_limited)
            self.assertEqual(count, i + 1)

    def test_exceed_limit_blocked(self):
        """Test requests exceeding limit are blocked"""
        cache_key = "test_key_exceed"

        # Make requests up to the limit
        for i in range(5):
            is_limited, count, reset_time = is_rate_limited(cache_key, 5, 3600)
            self.assertFalse(is_limited)

        # Next request should be blocked
        is_limited, count, reset_time = is_rate_limited(cache_key, 5, 3600)
        self.assertTrue(is_limited)
        self.assertEqual(count, 5)

    @patch("time.time")
    def test_sliding_window_cleanup(self, mock_time):
        """Test that old timestamps are cleaned up"""
        cache_key = "test_key_cleanup"

        # Set initial time
        mock_time.return_value = 1000

        # Make 3 requests
        for i in range(3):
            is_rate_limited(cache_key, 5, 60)  # 5 requests per minute

        # Move time forward by 61 seconds (past the window)
        mock_time.return_value = 1061

        # Next request should not be limited (old timestamps cleaned up)
        is_limited, count, reset_time = is_rate_limited(cache_key, 5, 60)
        self.assertFalse(is_limited)
        self.assertEqual(count, 1)  # Only current request

    @patch("judge.utils.ratelimit.cache")
    def test_cache_failure_allows_request(self, mock_cache):
        """Test that cache failures allow requests (fail-open)"""
        mock_cache.get.side_effect = Exception("Cache error")

        is_limited, count, reset_time = is_rate_limited("test_key", 5, 3600)

        self.assertFalse(is_limited)
        self.assertEqual(count, 0)


class CreateRateLimitResponseTestCase(TestCase):
    """Test rate limit response creation"""

    def setUp(self):
        self.factory = RequestFactory()

    def test_response_status_and_headers(self):
        """Test that response has correct status and headers"""
        request = self.factory.get("/")
        reset_time = int(time.time()) + 3600

        response = create_rate_limit_response(request, 30, 35, reset_time)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-RateLimit-Limit"], "30")
        self.assertEqual(response["X-RateLimit-Remaining"], "0")
        self.assertEqual(response["X-RateLimit-Reset"], str(reset_time))
        self.assertIn("Retry-After", response)


class RateLimitDecoratorTestCase(TestCase):
    """Test the ratelimit decorator"""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass"
        )
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_decorator_allows_within_limit(self):
        """Test decorator allows requests within limit"""

        @ratelimit(key="ip", rate="5/h")
        def test_view(request):
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.1"

        # Make 3 requests (within limit of 5)
        for i in range(3):
            response = test_view(request)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content.decode(), "OK")

    def test_decorator_blocks_over_limit(self):
        """Test decorator blocks requests over limit"""

        @ratelimit(key="ip", rate="2/h")
        def test_view(request):
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.2"

        # Make 2 requests (at limit)
        for i in range(2):
            response = test_view(request)
            self.assertEqual(response.status_code, 200)

        # Third request should be blocked
        response = test_view(request)
        self.assertEqual(response.status_code, 429)

    def test_method_filtering(self):
        """Test that method filtering works"""

        @ratelimit(key="ip", rate="1/h", method=["POST"])
        def test_view(request):
            return HttpResponse("OK")

        request_get = self.factory.get("/")
        request_post = self.factory.post("/")
        request_get.META["REMOTE_ADDR"] = "192.168.1.3"
        request_post.META["REMOTE_ADDR"] = "192.168.1.3"

        # GET requests should not be rate limited
        response = test_view(request_get)
        self.assertEqual(response.status_code, 200)

        response = test_view(request_get)
        self.assertEqual(response.status_code, 200)

        # POST request should be rate limited
        response = test_view(request_post)
        self.assertEqual(response.status_code, 200)

        # Second POST should be blocked
        response = test_view(request_post)
        self.assertEqual(response.status_code, 429)

    def test_user_key_with_authenticated_user(self):
        """Test user-based rate limiting with authenticated user"""

        @ratelimit(key="user", rate="2/h")
        def test_view(request):
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.user = self.user

        # Make 2 requests (at limit)
        for i in range(2):
            response = test_view(request)
            self.assertEqual(response.status_code, 200)

        # Third request should be blocked
        response = test_view(request)
        self.assertEqual(response.status_code, 429)

    def test_custom_key_function(self):
        """Test custom key function"""

        def custom_key(request):
            return f"custom_{request.META.get('HTTP_X_CUSTOM_ID', 'default')}"

        @ratelimit(key=custom_key, rate="1/h")
        def test_view(request):
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.META["HTTP_X_CUSTOM_ID"] = "test123"

        # First request should work
        response = test_view(request)
        self.assertEqual(response.status_code, 200)

        # Second request should be blocked
        response = test_view(request)
        self.assertEqual(response.status_code, 429)

    def test_block_false_allows_over_limit(self):
        """Test that block=False allows requests over limit"""

        @ratelimit(key="ip", rate="1/h", block=False)
        def test_view(request):
            # Check if rate limit status is available
            if hasattr(request, "rate_limit_status"):
                if request.rate_limit_status["limited"]:
                    return HttpResponse("Limited but allowed", status=200)
            return HttpResponse("OK")

        request = self.factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.4"

        # First request
        response = test_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "OK")

        # Second request should be allowed but marked as limited
        response = test_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Limited but allowed")

    def test_invalid_rate_raises_error(self):
        """Test that invalid rate format raises ValueError"""
        with self.assertRaises(ValueError):

            @ratelimit(key="ip", rate="invalid")
            def test_view(request):
                return HttpResponse("OK")

    def test_missing_rate_raises_error(self):
        """Test that missing rate raises ValueError"""
        with self.assertRaises(ValueError):

            @ratelimit(key="ip")
            def test_view(request):
                return HttpResponse("OK")
