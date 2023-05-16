import time
import logging
import random
import json
from datetime import datetime

from django.conf import settings
from django.http import HttpResponseRedirect, Http404
from django.urls import Resolver404, resolve, reverse
from django.utils.http import urlquote
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import gettext as _

from judge.models import Organization
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


class DMOJLoginMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            profile = request.profile = request.user.profile
            login_2fa_path = reverse("login_2fa")
            if (
                profile.is_totp_enabled
                and not request.session.get("2fa_passed", False)
                and request.path not in (login_2fa_path, reverse("auth_logout"))
                and not request.path.startswith(settings.STATIC_URL)
            ):
                return HttpResponseRedirect(
                    login_2fa_path + "?next=" + urlquote(request.get_full_path())
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
            request.contest_mode = request.session.get("contest_mode", True)
        else:
            request.in_contest = False
            request.participation = None
        request.in_contest_mode = request.in_contest and request.contest_mode
        return self.get_response(request)


class DarkModeMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if "darkmode" in request.GET:
            return HttpResponseRedirect(
                reverse("toggle_darkmode") + "?next=" + urlquote(request.path)
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
                        reverse("auth_login") + "?next=" + urlquote(request.path)
                    )
        except ObjectDoesNotExist:
            return generic_message(
                request,
                _("No such group"),
                _("No such group"),
                status=404,
            )
        return self.get_response(request)


class SlowRequestMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        logger = logging.getLogger("judge.request_time")
        logger_slow = logging.getLogger("judge.slow_request")
        start_time = time.time()
        response = self.get_response(request)
        if response.status_code == 200:
            try:
                response_time = time.time() - start_time
                url_name = resolve(request.path).url_name
                message = {
                    "url_name": url_name,
                    "response_time": response_time * 1000,
                    "profile": request.user.username,
                    "date": datetime.now().strftime("%Y/%m/%d"),
                    "url": request.build_absolute_uri(),
                    "method": request.method,
                }
                if response_time > 9:
                    logger_slow.info(json.dumps(message))
                if random.random() < 0.1:
                    logger.info(json.dumps(message))
            except Exception:
                pass
        return response
