from inspect import signature
from django.core.cache import cache, caches
from django.db.models.query import QuerySet
from django.core.handlers.wsgi import WSGIRequest

import hashlib

MAX_NUM_CHAR = 50
NONE_RESULT = "__None__"


def arg_to_str(arg):
    if hasattr(arg, "id"):
        return str(arg.id)
    if isinstance(arg, list) or isinstance(arg, QuerySet):
        return hashlib.sha1(str(list(arg)).encode()).hexdigest()[:MAX_NUM_CHAR]
    if len(str(arg)) > MAX_NUM_CHAR:
        return str(arg)[:MAX_NUM_CHAR]
    return str(arg)


def filter_args(args_list):
    return [x for x in args_list if not isinstance(x, WSGIRequest)]


l0_cache = caches["l0"] if "l0" in caches else None


def cache_wrapper(prefix, timeout=None):
    def get_key(func, *args, **kwargs):
        args_list = list(args)
        signature_args = list(signature(func).parameters.keys())
        args_list += [kwargs.get(k) for k in signature_args[len(args) :]]
        args_list = filter_args(args_list)
        args_list = [arg_to_str(i) for i in args_list]
        key = prefix + ":" + ":".join(args_list)
        key = key.replace(" ", "_")
        return key

    def _get(key):
        if not l0_cache:
            return cache.get(key)
        result = l0_cache.get(key)
        if result is None:
            result = cache.get(key)
        return result

    def _set_l0(key, value):
        if l0_cache:
            l0_cache.set(key, value, 30)

    def _set(key, value, timeout):
        _set_l0(key, value)
        cache.set(key, value, timeout)

    def decorator(func):
        def wrapper(*args, **kwargs):
            cache_key = get_key(func, *args, **kwargs)
            result = _get(cache_key)
            if result is not None:
                _set_l0(cache_key, result)
                if type(result) == str and result == NONE_RESULT:
                    result = None
                return result
            result = func(*args, **kwargs)
            if result is None:
                result = NONE_RESULT
            _set(cache_key, result, timeout)
            return result

        def dirty(*args, **kwargs):
            cache_key = get_key(func, *args, **kwargs)
            cache.delete(cache_key)
            if l0_cache:
                l0_cache.delete(cache_key)

        wrapper.dirty = dirty

        return wrapper

    return decorator
