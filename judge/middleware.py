import random
import time
from urllib.parse import quote, urlparse

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection
from django.http import HttpResponseRedirect
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.cache_handler import (
    clear_request_l0_cache,
    start_request_cache_profile,
    stop_request_cache_profile,
)
from judge.models import Course, Language, Organization, Profile, RequestMetric
from judge.utils.views import generic_message

USED_DOMAINS = ["www"]
URL_NAMES_BYPASS_SUBDOMAIN = ["submission_source_file"]


class ShortCircuitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            callback, args, kwargs = resolve(
                request.path_info, getattr(request, "urlconf", None)
            )
        except Resolver404:
            callback, args, kwargs = None, None, None

        if getattr(callback, "short_circuit_middleware", False):
            return callback(request, *args, **kwargs)
        return self.get_response(request)


class InactiveUserLogoutMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not user.is_active:
            logout(request)
        return self.get_response(request)


class DMOJLoginMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                profile = request.profile = request.user.profile
            except User.profile.RelatedObjectDoesNotExist:
                profile, _ = Profile.objects.get_or_create(
                    user=request.user,
                    defaults={
                        "language_id": Language.get_default_language(),
                    },
                )
                request.profile = profile

            if (
                profile.mute
                and profile.mute_until
                and profile.mute_until <= timezone.now()
            ):
                profile.mute = False
                profile.mute_until = None
                profile.mute_reason = ""
                profile.save(update_fields=["mute", "mute_until", "mute_reason"])
                Profile.dirty_cache(profile.id)

            login_2fa_path = reverse("login_2fa")
            if (
                profile.is_totp_enabled
                and not request.session.get("2fa_passed", False)
                and request.path not in (login_2fa_path, reverse("auth_logout"))
                and not request.path.startswith(settings.STATIC_URL)
            ):
                return HttpResponseRedirect(
                    login_2fa_path + "?next=" + quote(request.get_full_path())
                )
        else:
            request.profile = None
        return self.get_response(request)


class DMOJImpersonationMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_impersonate:
            request.no_profile_update = True
            request.profile = request.user.profile
        return self.get_response(request)


class ContestMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        profile = request.profile
        if profile:
            profile.update_contest()
            request.participation = profile.current_contest
            request.in_contest = request.participation is not None
        else:
            request.in_contest = False
            request.participation = None
        return self.get_response(request)


class DarkModeMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if "darkmode" in request.GET:
            return HttpResponseRedirect(
                reverse("toggle_darkmode") + "?next=" + quote(request.path)
            )
        return self.get_response(request)


class SubdomainMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        if not settings.USE_SUBDOMAIN:
            return self.get_response(request)

        domain = request.get_host()
        site = get_current_site(request).domain
        subdomain = domain[: len(domain) - len(site)].lower()

        if len(subdomain) <= 1:
            return self.get_response(request)

        subdomain = subdomain[:-1]

        if (
            subdomain in USED_DOMAINS
            or resolve(request.path).url_name in URL_NAMES_BYPASS_SUBDOMAIN
        ):
            return self.get_response(request)

        try:
            organization = Organization.objects.get(slug=subdomain)
            if request.profile and organization in request.profile.organizations.all():
                request.organization = organization
            else:
                if request.profile:
                    return generic_message(
                        request,
                        _("No permission"),
                        _("You need to join this group first"),
                        status=404,
                    )
                if not request.GET.get("next", None):
                    return HttpResponseRedirect(
                        reverse("auth_login") + "?next=" + quote(request.path)
                    )
        except ObjectDoesNotExist:
            return generic_message(
                request,
                _("No such group"),
                _("No such group"),
                status=404,
            )
        return self.get_response(request)


class CourseMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.course = None
        try:
            # Check if the URL is a course-related path
            resolved = resolve(request.path)
            if "slug" in resolved.kwargs and request.path.startswith("/course/"):
                course_slug = resolved.kwargs["slug"]
                try:
                    course = Course.objects.get(slug=course_slug)
                    # Only set request.course if user has access to the course
                    # getattr handles the case where request.profile might not exist yet
                    profile = getattr(request, "profile", None)
                    if Course.is_accessible_by(course, profile):
                        request.course = course
                except Course.DoesNotExist:
                    pass
        except Resolver404:
            pass
        return self.get_response(request)


class SlowRequestMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response
        self.sample_rate = self._get_rate_setting("REQUEST_METRICS_SAMPLE_RATE", 0.1)
        self.collect_db_timing = getattr(
            settings, "REQUEST_METRICS_COLLECT_DB_TIMING", True
        )
        self.collect_cache_timing = getattr(
            settings, "REQUEST_METRICS_COLLECT_CACHE_TIMING", True
        )
        self.profile_sample_rate = self._get_rate_setting(
            "REQUEST_METRICS_PROFILE_SAMPLE_RATE", 0.01
        )
        self.cache_profile_sample_rate = self._get_rate_setting(
            "REQUEST_METRICS_CACHE_PROFILE_SAMPLE_RATE", 0.01
        )
        self.slow_threshold_seconds = getattr(
            settings, "SLOW_REQUEST_THRESHOLD_SECONDS", 5
        )
        self.max_profiler_queries = getattr(
            settings, "REQUEST_METRICS_MAX_PROFILER_QUERIES", 5
        )
        self.max_cache_profiler_operations = getattr(
            settings, "REQUEST_METRICS_MAX_CACHE_PROFILER_OPERATIONS", 5
        )

    def __call__(self, request):
        start_time = time.perf_counter()
        profiler = None
        cache_profiler = None
        if self.collect_cache_timing:
            start_request_cache_profile(
                capture_details=self._sample(self.cache_profile_sample_rate),
                max_operations=self.max_cache_profiler_operations,
            )

        try:
            if self.collect_db_timing:
                profiler = RequestQueryProfiler(
                    capture_details=self._sample(self.profile_sample_rate),
                    max_queries=self.max_profiler_queries,
                )
                with connection.execute_wrapper(profiler):
                    response = self.get_response(request)
            else:
                response = self.get_response(request)
        finally:
            if self.collect_cache_timing:
                cache_profiler = stop_request_cache_profile()

        if not self._should_record_request(request, response):
            return response

        try:
            response_time = time.perf_counter() - start_time
            is_slow = response_time >= self.slow_threshold_seconds
            if is_slow or self._sample(self.sample_rate):
                self._record_metric(
                    request, response, response_time, profiler, cache_profiler
                )
        except Exception:
            pass
        return response

    def _get_rate_setting(self, setting_name, default):
        value = getattr(settings, setting_name, None)
        if value is None and setting_name == "REQUEST_METRICS_SAMPLE_RATE":
            value = getattr(settings, "REQUEST_TIME_SAMPLE_RATE", None)
        if value is None:
            value = default
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return max(0, min(value, 1))

    def _sample(self, rate):
        return rate >= 1 or random.random() < rate

    def _should_record_request(self, request, response):
        if not (0 < self.sample_rate or self.slow_threshold_seconds > 0):
            return False
        if getattr(response, "streaming", False):
            return False
        if getattr(request, "path", "").startswith("/internal/"):
            return False
        return True

    def _record_metric(
        self, request, response, response_time, profiler=None, cache_profiler=None
    ):
        resolved = None
        try:
            resolved = resolve(request.path_info, getattr(request, "urlconf", None))
        except Exception:
            pass

        profiler_data = {}
        if profiler is not None:
            profiler_data.update(profiler.as_dict())
        if cache_profiler is not None:
            profiler_data.update(cache_profiler.as_dict())

        user = getattr(request, "user", None)
        RequestMetric.objects.create(
            url_name=resolved.url_name if resolved else None,
            response_time_ms=response_time * 1000,
            is_authenticated=user is not None and user.is_authenticated,
            username=(
                user.username if user is not None and user.is_authenticated else ""
            ),
            full_url=request.build_absolute_uri(),
            path=request.get_full_path(),
            method=request.method,
            status_code=response.status_code,
            db_query_count=profiler.query_count if profiler is not None else None,
            db_time_ms=profiler.db_time_ms if profiler is not None else None,
            cache_call_count=(
                cache_profiler.call_count if cache_profiler is not None else None
            ),
            cache_time_ms=(
                cache_profiler.total_time_ms if cache_profiler is not None else None
            ),
            profiler=profiler_data,
        )


class RequestQueryProfiler:
    def __init__(self, capture_details=False, max_queries=5):
        self.capture_details = capture_details
        self.max_queries = max_queries
        self.query_count = 0
        self.db_time_ms = 0
        self.slowest_queries = []

    def __call__(self, execute, sql, params, many, context):
        start_time = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.query_count += 1
            self.db_time_ms += elapsed_ms
            if self.capture_details and self.max_queries > 0:
                self._record_query(sql, elapsed_ms, many)

    def _record_query(self, sql, elapsed_ms, many):
        self.slowest_queries.append(
            {
                "sql": str(sql)[:1000],
                "time_ms": elapsed_ms,
                "many": bool(many),
            }
        )
        self.slowest_queries = sorted(
            self.slowest_queries, key=lambda query: query["time_ms"], reverse=True
        )[: self.max_queries]

    def as_dict(self):
        if not self.slowest_queries:
            return {}
        return {"slowest_queries": self.slowest_queries}


class RequestScopedCacheMiddleware:
    """
    Middleware to clear request-scoped L0 cache at the end of each request.
    This ensures that the L0 cache is only valid within a single request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Clear the request-scoped L0 cache after processing the request
        clear_request_l0_cache()
        return response


class ContentSecurityPolicyMiddleware:
    """Set a Content-Security-Policy that restricts embedded frame sources.

    Only the ``frame-src`` directive is emitted, so nothing else on the page is
    restricted (no ``default-src``). User-authored markdown iframes are
    sanitized against ``settings.IFRAME_ALLOWED_HOSTS``. App-controlled embeds
    can add hosts via ``settings.CSP_FRAME_ALLOWED_HOSTS``; PDF descriptions may
    also need the configured media origin when files are served from remote
    storage/CDN.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        hosts = list(getattr(settings, "IFRAME_ALLOWED_HOSTS", [])) + list(
            getattr(settings, "CSP_FRAME_ALLOWED_HOSTS", [])
        )
        media_origin = self._media_origin()
        sources = ["'self'"] + ["https://%s" % h for h in hosts]
        if media_origin and media_origin not in sources:
            sources.append(media_origin)
        self.policy = "frame-src " + " ".join(sources)

    def _media_origin(self):
        media_url = getattr(settings, "MEDIA_URL", "")
        parsed = urlparse(media_url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return "%s://%s" % (parsed.scheme, parsed.netloc)
        return None

    def __call__(self, request):
        response = self.get_response(request)
        # Do not override a policy already set further up the stack.
        response.setdefault("Content-Security-Policy", self.policy)
        return response
