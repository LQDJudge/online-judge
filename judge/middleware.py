from django.conf import settings
from django.http import HttpResponseRedirect, Http404
from django.urls import Resolver404, resolve, reverse
from django.utils.http import urlquote
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ObjectDoesNotExist

from judge.models import Organization


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
        domain = request.get_host()
        site = get_current_site(request).domain
        subdomain = domain[: len(domain) - len(site)]
        request.organization = None
        if len(subdomain) > 1:
            subdomain = subdomain[:-1]
            try:
                organization = Organization.objects.get(slug=subdomain)
                if (
                    request.profile
                    and organization in request.profile.organizations.all()
                ):
                    request.organization = organization
                else:
                    if request.profile:
                        raise Http404
                    if not request.GET.get("next", None):
                        return HttpResponseRedirect(
                            reverse("auth_login") + "?next=" + urlquote(request.path)
                        )
            except ObjectDoesNotExist:
                pass
        return self.get_response(request)
