import re
from functools import partial

from django.conf import settings
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.sites.shortcuts import get_current_site
from django.core.cache import cache
from django.utils.functional import SimpleLazyObject, new_method_proxy

from mptt.querysets import TreeQuerySet

from .models import MiscConfig, NavigationBar, Profile
from judge.caching import cache_wrapper


class FixedSimpleLazyObject(SimpleLazyObject):
    if not hasattr(SimpleLazyObject, "__iter__"):
        __iter__ = new_method_proxy(iter)


def get_resource(request):
    use_https = settings.DMOJ_SSL
    if use_https == 1:
        scheme = "https" if request.is_secure() else "http"
    elif use_https > 1:
        scheme = "https"
    else:
        scheme = "http"

    return {
        "INLINE_JQUERY": settings.INLINE_JQUERY,
        "INLINE_FONTAWESOME": settings.INLINE_FONTAWESOME,
        "JQUERY_JS": settings.JQUERY_JS,
        "FONTAWESOME_CSS": settings.FONTAWESOME_CSS,
        "DMOJ_SCHEME": scheme,
        "DMOJ_CANONICAL": settings.DMOJ_CANONICAL,
        "use_darkmode": request.session.get("darkmode", False) == True,
    }


def get_profile(request):
    if request.user.is_authenticated:
        return Profile.objects.get_or_create(user=request.user)[0]
    return None


def comet_location(request):
    if request.is_secure():
        websocket = getattr(settings, "EVENT_DAEMON_URL_SSL", settings.EVENT_DAEMON_URL)
    else:
        websocket = settings.EVENT_DAEMON_URL
    return {"EVENT_DAEMON_LOCATION": websocket}


@cache_wrapper(prefix="nb", expected_type=TreeQuerySet)
def _nav_bar():
    return NavigationBar.objects.all()


def __nav_tab(path):
    nav_bar_list = list(_nav_bar())
    nav_bar_dict = {nb.id: nb for nb in nav_bar_list}
    result = next((nb for nb in nav_bar_list if re.match(nb.regex, path)), None)
    if result:
        while result.parent_id:
            result = nav_bar_dict.get(result.parent_id)
        return result.key
    else:
        return []


def general_info(request):
    path = request.get_full_path()
    return {
        "nav_tab": FixedSimpleLazyObject(partial(__nav_tab, request.path)),
        "nav_bar": _nav_bar(),
        "LOGIN_RETURN_PATH": "" if path.startswith("/accounts/") else path,
        "perms": PermWrapper(request.user),
    }


def site(request):
    return {"site": get_current_site(request)}


class MiscConfigDict(dict):
    __slots__ = ("language", "site")

    def __init__(self, language="", domain=None):
        self.language = language
        self.site = domain
        super(MiscConfigDict, self).__init__()

    def __missing__(self, key):
        cache_key = "misc_config:%s:%s:%s" % (self.site, self.language, key)
        value = cache.get(cache_key)
        if value is None:
            keys = ["%s.%s" % (key, self.language), key] if self.language else [key]
            if self.site is not None:
                keys = ["%s:%s" % (self.site, key) for key in keys] + keys
            map = dict(
                MiscConfig.objects.values_list("key", "value").filter(key__in=keys)
            )
            for item in keys:
                if item in map:
                    value = map[item]
                    break
            else:
                value = ""
            cache.set(cache_key, value, 86400)
        self[key] = value
        return value


def misc_config(request):
    domain = get_current_site(request).domain
    return {
        "misc_config": MiscConfigDict(domain=domain),
        "i18n_config": MiscConfigDict(language=request.LANGUAGE_CODE, domain=domain),
    }


def site_name(request):
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_LONG_NAME": settings.SITE_LONG_NAME,
        "SITE_ADMIN_EMAIL": settings.SITE_ADMIN_EMAIL,
    }
