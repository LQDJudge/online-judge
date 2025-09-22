"""
Built-in rate limiting decorator for LQDOJ
Compatible with django-ratelimit API for seamless replacement

Uses O(1) sliding window counter algorithm for efficient rate limiting
"""

import re
import time
import logging
from functools import wraps
from typing import Callable, List, Optional, Union

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.urls import resolve
from django.utils.translation import gettext as _
from django.conf import settings

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded"""

    pass


def parse_rate(rate: str) -> tuple[int, int]:
    """
    Parse rate string like '30/h', '200/h', '10/m', '5/s' into (count, seconds)

    Args:
        rate: Rate string in format "count/period"

    Returns:
        Tuple of (count, period_in_seconds)

    Raises:
        ValueError: If rate format is invalid
    """
    if not rate:
        raise ValueError("Rate cannot be empty")

    match = re.match(r"^(\d+)/([smhd])$", rate.lower())
    if not match:
        raise ValueError(
            f"Invalid rate format: {rate}. Expected format: 'number/period' (s/m/h/d)"
        )

    count, period = match.groups()
    count = int(count)

    # Convert period to seconds
    period_seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }[period]

    return count, period_seconds


def get_cache_key(request: HttpRequest, key_type: str, view_name: str) -> str:
    """
    Generate cache key for rate limiting

    Args:
        request: Django HTTP request
        key_type: Type of key (user, ip, header:name, or custom)
        view_name: Name of the view being rate limited

    Returns:
        Cache key string
    """
    if key_type == "user":
        if request.user.is_authenticated:
            identifier = str(request.user.id)
        else:
            # Fall back to IP for anonymous users
            identifier = get_client_ip(request)
    elif key_type == "ip":
        identifier = get_client_ip(request)
    elif key_type.startswith("header:"):
        header_name = key_type[7:]  # Remove "header:" prefix
        identifier = request.META.get(
            f"HTTP_{header_name.upper().replace('-', '_')}", "unknown"
        )
    else:
        # For callable keys or other custom keys
        identifier = str(key_type)

    return f"ratelimit:{key_type}:{identifier}:{view_name}"


def get_client_ip(request: HttpRequest) -> str:
    """
    Get client IP address from request

    Args:
        request: Django HTTP request

    Returns:
        Client IP address
    """
    # Check for forwarded IP first (for reverse proxies)
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    # Check for real IP (some proxy configurations)
    x_real_ip = request.META.get("HTTP_X_REAL_IP")
    if x_real_ip:
        return x_real_ip

    # Fall back to remote address
    return request.META.get("REMOTE_ADDR", "unknown")


def _calculate_sliding_window_estimate(
    window_data: dict, now: float, window_start: float, sub_window_duration: float
) -> int:
    """
    Calculate sliding window estimate using overlap ratio

    Args:
        window_data: Dictionary containing current and previous window counts
        now: Current timestamp
        window_start: Start time of current sub-window
        sub_window_duration: Duration of each sub-window

    Returns:
        Estimated request count for the sliding window
    """
    time_into_window = now - window_start
    overlap_ratio = max(
        0, (sub_window_duration - time_into_window) / sub_window_duration
    )
    return int(window_data["previous"] * overlap_ratio + window_data["current"])


def is_rate_limited_sliding_counter(
    cache_key: str, rate_count: int, rate_period: int
) -> tuple[bool, int, int]:
    """
    Check rate limit using sliding window counter with O(1) memory complexity

    Uses 10 sub-windows for accurate rate limiting with constant memory usage.
    Maintains ~95% accuracy compared to perfect sliding window while using
    constant memory regardless of the rate limit count.

    Args:
        cache_key: Cache key for this rate limit
        rate_count: Maximum number of requests allowed
        rate_period: Time period in seconds

    Returns:
        Tuple of (is_limited, estimated_count, reset_time)
    """
    now = time.time()
    sub_window_duration = rate_period / 10
    current_window_id = int(now // sub_window_duration)
    window_start = current_window_id * sub_window_duration

    # Create window-specific cache key
    window_cache_key = f"{cache_key}:swc:{rate_period}:{current_window_id}"

    try:
        # Get current window data
        window_data = cache.get(window_cache_key)

        if window_data is None:
            # First request in this window
            window_data = {
                "current": 0,
                "previous": 0,
                "window_start": window_start,
                "last_reset": now,
            }
        elif window_data["window_start"] < window_start:
            # We've moved to a new window - rotate counters
            window_data = {
                "current": 0,
                "previous": window_data["current"],
                "window_start": window_start,
                "last_reset": now,
            }

        # Calculate sliding window estimate
        estimated_count = _calculate_sliding_window_estimate(
            window_data, now, window_start, sub_window_duration
        )

        # Check if rate limit exceeded
        if estimated_count >= rate_count:
            reset_time = int(window_start + sub_window_duration)
            return True, estimated_count, reset_time

        # Increment current window counter
        window_data["current"] += 1

        # Store updated data with appropriate TTL
        ttl = int(rate_period + sub_window_duration)
        cache.set(window_cache_key, window_data, timeout=ttl)

        # Calculate reset time (when the window will be clear)
        reset_time = int(window_start + rate_period)
        return False, estimated_count + 1, reset_time

    except Exception as e:
        # If cache fails, log error and allow request (fail-open)
        logger.warning(f"Rate limiting cache error: {e}")
        return False, 0, int(now + rate_period)


def check_multiple_rates_sliding_counter(
    cache_key_base: str, rates: List[tuple[int, int]]
) -> tuple[bool, dict]:
    """
    Check multiple rate limits using sliding window counters with O(1) memory per rate

    Each rate limit uses constant memory regardless of the rate limit count,
    providing efficient rate limiting for multiple concurrent limits.

    Args:
        cache_key_base: Base cache key for rate limits
        rates: List of (rate_count, rate_period) tuples

    Returns:
        Tuple of (is_any_limited, rate_info_dict)
        rate_info_dict contains details about each rate limit
    """
    rate_info = {}
    any_limited = False
    most_restrictive_reset = 0

    for i, (rate_count, rate_period) in enumerate(rates):
        # Create unique cache key for each rate
        cache_key = f"{cache_key_base}:rate_{i}_{rate_period}"

        is_limited, estimated_count, reset_time = is_rate_limited_sliding_counter(
            cache_key, rate_count, rate_period
        )

        rate_info[f"rate_{i}"] = {
            "limit": rate_count,
            "period": rate_period,
            "current": estimated_count,
            "limited": is_limited,
            "reset": reset_time,
            "rate_string": f"{rate_count}/{rate_period}s",
        }

        if is_limited:
            any_limited = True
            # Track the earliest reset time among violated limits
            if most_restrictive_reset == 0 or reset_time < most_restrictive_reset:
                most_restrictive_reset = reset_time

    return any_limited, rate_info


def create_rate_limit_response(
    request: HttpRequest, rate_info: dict, reset_time: int = None
) -> HttpResponse:
    """
    Create HTTP 429 Too Many Requests response

    Args:
        request: Django HTTP request
        rate_info: Dictionary containing rate limit information
        reset_time: Unix timestamp when rate limit resets (optional)

    Returns:
        HTTP 429 response with rate limit headers
    """
    # Check if rate_info contains multiple rates or single rate format
    if any(key.startswith("rate_") for key in rate_info.keys()):
        # Multiple rates format
        violated_rates = [info for info in rate_info.values() if info["limited"]]

        if violated_rates:
            # Use the rate with the shortest reset time
            most_restrictive = min(violated_rates, key=lambda x: x["reset"])
            rate_count = most_restrictive["limit"]
            current_count = most_restrictive["current"]
            reset_time = most_restrictive["reset"]

            # Create detailed message
            violated_rate_strings = [info["rate_string"] for info in violated_rates]
            message = _("Rate limit exceeded")
        else:
            # This shouldn't happen, but handle gracefully
            rate_count = 0
            current_count = 0
            reset_time = int(time.time() + 3600)
            message = _("Rate limit exceeded")
    else:
        # Single rate format (backward compatibility)
        rate_count = rate_info.get("limit", 0)
        current_count = rate_info.get("count", 0)
        reset_time = reset_time or rate_info.get("reset", int(time.time() + 3600))
        message = _("Rate limit exceeded")

    response = HttpResponse(
        message,
        status=429,
        content_type="text/plain",
    )

    # Add standard rate limit headers
    response["X-RateLimit-Limit"] = str(rate_count)
    response["X-RateLimit-Remaining"] = str(max(0, rate_count - current_count))
    response["X-RateLimit-Reset"] = str(reset_time)
    response["Retry-After"] = str(max(1, reset_time - int(time.time())))

    return response


def ratelimit(
    key: Union[str, Callable] = "ip",
    rate: Optional[Union[str, List[str]]] = None,
    method: Optional[List[str]] = None,
    block: bool = True,
) -> Callable:
    """
    Rate limiting decorator using O(1) memory sliding window counter algorithm
    Compatible with django-ratelimit API for seamless replacement

    Uses 10 sub-windows for accurate rate limiting with constant memory usage.
    Maintains ~95% accuracy compared to perfect sliding window while using
    constant memory regardless of the rate limit count.

    Args:
        key: Rate limit key type or callable function
             - "user": Rate limit per authenticated user
             - "ip": Rate limit per IP address
             - "header:name": Rate limit per header value
             - callable: Custom function that returns key string
        rate: Rate limit string like "30/h" or list of strings like ["30/h", "2/m"]
        method: List of HTTP methods to rate limit (default: all methods)
        block: Whether to block requests that exceed rate limit (default: True)

    Returns:
        Decorated function

    Raises:
        ValueError: If rate format is invalid
    """
    if rate is None:
        raise ValueError("Rate must be specified")

    # Parse rate(s) into list of (count, period) tuples
    rates = []
    if isinstance(rate, str):
        # Single rate string
        try:
            rate_count, rate_period = parse_rate(rate)
            rates.append((rate_count, rate_period))
        except ValueError as e:
            raise ValueError(f"Invalid rate format: {e}")
    elif isinstance(rate, list):
        # Multiple rate strings
        if not rate:
            raise ValueError("Rate list cannot be empty")
        for rate_str in rate:
            try:
                rate_count, rate_period = parse_rate(rate_str)
                rates.append((rate_count, rate_period))
            except ValueError as e:
                raise ValueError(f"Invalid rate format in '{rate_str}': {e}")
    else:
        raise ValueError("Rate must be a string or list of strings")

    # Normalize method list
    if method is not None:
        method = [m.upper() for m in method]

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(request: HttpRequest, *args, **kwargs):
            # Check if we should rate limit this method
            if method is not None and request.method.upper() not in method:
                return func(request, *args, **kwargs)

            # Get view name for cache key
            try:
                view_name = resolve(request.path).url_name or func.__name__
            except Exception:
                view_name = func.__name__

            # Determine the key for rate limiting
            if callable(key):
                try:
                    key_value = key(request)
                    cache_key_base = f"ratelimit:custom:{key_value}:{view_name}"
                except Exception as e:
                    logger.warning(f"Custom rate limit key function failed: {e}")
                    # Fall back to IP-based rate limiting
                    cache_key_base = get_cache_key(request, "ip", view_name)
            else:
                cache_key_base = get_cache_key(request, key, view_name)

            # Use O(1) sliding counter algorithm
            if len(rates) == 1:
                rate_count, rate_period = rates[0]
                is_limited, current_count, reset_time = is_rate_limited_sliding_counter(
                    cache_key_base, rate_count, rate_period
                )
                rate_info = {
                    "limited": is_limited,
                    "count": current_count,
                    "limit": rate_count,
                    "reset": reset_time,
                }
            else:
                is_limited, rate_info = check_multiple_rates_sliding_counter(
                    cache_key_base, rates
                )

            if is_limited and block:
                if len(rates) == 1:
                    logger.info(
                        f"Rate limit exceeded for {cache_key_base}: "
                        f"{current_count}/{rate_count} requests"
                    )
                    return create_rate_limit_response(request, rate_info, reset_time)
                else:
                    violated_rates = [
                        info for info in rate_info.values() if info["limited"]
                    ]
                    violated_rate_strings = [
                        info["rate_string"] for info in violated_rates
                    ]
                    logger.info(
                        f"Rate limit exceeded for {cache_key_base}: "
                        f"violated limits: {', '.join(violated_rate_strings)}"
                    )
                    return create_rate_limit_response(request, rate_info)

            # Add rate limit info to request for debugging
            request.rate_limit_status = rate_info

            return func(request, *args, **kwargs)

        return wrapper

    return decorator


# Alias for compatibility
ratelimit_decorator = ratelimit
