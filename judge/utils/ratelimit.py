"""
Built-in rate limiting decorator for LQDOJ
Compatible with django-ratelimit API for seamless replacement
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


def is_rate_limited(
    cache_key: str, rate_count: int, rate_period: int
) -> tuple[bool, int, int]:
    """
    Check if rate limit is exceeded using sliding window approach

    Args:
        cache_key: Cache key for this rate limit
        rate_count: Maximum number of requests allowed
        rate_period: Time period in seconds

    Returns:
        Tuple of (is_limited, current_count, reset_time)
    """
    now = time.time()
    window_start = now - rate_period

    try:
        # Get current timestamps from cache
        timestamps = cache.get(cache_key, [])

        # Remove expired timestamps (outside the window)
        timestamps = [ts for ts in timestamps if ts > window_start]

        # Check if we're over the limit
        if len(timestamps) >= rate_count:
            # Calculate when the oldest request will expire
            reset_time = int(timestamps[0] + rate_period)
            return True, len(timestamps), reset_time

        # Add current timestamp
        timestamps.append(now)

        # Store back in cache with TTL slightly longer than the period
        cache.set(cache_key, timestamps, timeout=rate_period + 60)

        # Calculate reset time (when the window will be clear)
        reset_time = int(now + rate_period)
        return False, len(timestamps), reset_time

    except Exception as e:
        # If cache fails, log error and allow request (fail-open)
        logger.warning(f"Rate limiting cache error: {e}")
        return False, 0, int(now + rate_period)


def create_rate_limit_response(
    request: HttpRequest, rate_count: int, current_count: int, reset_time: int
) -> HttpResponse:
    """
    Create HTTP 429 Too Many Requests response

    Args:
        request: Django HTTP request
        rate_count: Maximum requests allowed
        current_count: Current request count
        reset_time: Unix timestamp when rate limit resets

    Returns:
        HTTP 429 response with rate limit headers
    """
    response = HttpResponse(
        _("Rate limit exceeded. Too many requests."),
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
    rate: Optional[str] = None,
    method: Optional[List[str]] = None,
    block: bool = True,
) -> Callable:
    """
    Rate limiting decorator compatible with django-ratelimit API

    Args:
        key: Rate limit key type or callable function
             - "user": Rate limit per authenticated user
             - "ip": Rate limit per IP address
             - "header:name": Rate limit per header value
             - callable: Custom function that returns key string
        rate: Rate limit string like "30/h", "200/h", "10/m", "5/s"
        method: List of HTTP methods to rate limit (default: all methods)
        block: Whether to block requests that exceed rate limit (default: True)

    Returns:
        Decorated function

    Raises:
        ValueError: If rate format is invalid
    """
    if rate is None:
        raise ValueError("Rate must be specified")

    # Parse rate into count and period
    try:
        rate_count, rate_period = parse_rate(rate)
    except ValueError as e:
        raise ValueError(f"Invalid rate format: {e}")

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
                    cache_key = f"ratelimit:custom:{key_value}:{view_name}"
                except Exception as e:
                    logger.warning(f"Custom rate limit key function failed: {e}")
                    # Fall back to IP-based rate limiting
                    cache_key = get_cache_key(request, "ip", view_name)
            else:
                cache_key = get_cache_key(request, key, view_name)

            # Check rate limit
            is_limited, current_count, reset_time = is_rate_limited(
                cache_key, rate_count, rate_period
            )

            if is_limited and block:
                logger.info(
                    f"Rate limit exceeded for {cache_key}: "
                    f"{current_count}/{rate_count} requests"
                )
                return create_rate_limit_response(
                    request, rate_count, current_count, reset_time
                )

            # Add rate limit info to request for debugging
            request.rate_limit_status = {
                "limited": is_limited,
                "count": current_count,
                "limit": rate_count,
                "reset": reset_time,
            }

            return func(request, *args, **kwargs)

        return wrapper

    return decorator


# Alias for compatibility
ratelimit_decorator = ratelimit
