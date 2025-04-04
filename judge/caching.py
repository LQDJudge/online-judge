from django.core.cache import cache
from django.db.models.query import QuerySet
from django.core.handlers.wsgi import WSGIRequest
from django.db import models
from django.db.models.query_utils import DeferredAttribute

import hashlib
from inspect import signature

MAX_NUM_CHAR = 50
NONE_RESULT = "__None__"  # Placeholder for None values in caching


# Utility functions
def arg_to_str(arg):
    """Convert arguments to strings for generating cache keys."""
    if hasattr(arg, "id"):
        return str(arg.id)
    if isinstance(arg, list) or isinstance(arg, QuerySet):
        return hashlib.sha1(str(list(arg)).encode()).hexdigest()[:MAX_NUM_CHAR]
    if len(str(arg)) > MAX_NUM_CHAR:
        return str(arg)[:MAX_NUM_CHAR]
    return str(arg)


def filter_args(args_list):
    """Filter out arguments that are not relevant for caching (e.g., WSGIRequest)."""
    return [x for x in args_list if not isinstance(x, WSGIRequest)]


def cache_wrapper(prefix, timeout=None, expected_type=None, batch_fn=None):
    def get_key(func, *args, **kwargs):
        args_list = list(args)
        signature_args = list(signature(func).parameters.keys())
        args_list += [kwargs.get(k) for k in signature_args[len(args) :]]
        args_list = filter_args(args_list)
        args_list = [arg_to_str(i) for i in args_list]
        key = prefix + ":" + ":".join(args_list)
        key = key.replace(" ", "_")
        return key

    def decorator(func):
        def _validate_type(cache_key, result):
            if expected_type and not isinstance(result, expected_type):
                return False
            return True

        def wrapper(*args, **kwargs):
            cache_key = get_key(func, *args, **kwargs)
            result = cache.get(cache_key)

            if result is not None and _validate_type(cache_key, result):
                if type(result) == str and result == NONE_RESULT:
                    result = None
                return result

            # Call the original function
            result = func(*args, **kwargs)
            cache.set(cache_key, NONE_RESULT if result is None else result, timeout)
            return result

        def dirty(*args, **kwargs):
            cache_key = get_key(func, *args, **kwargs)
            cache.delete(cache_key)

        def batch(args_list):
            """
            Process multiple function calls efficiently using batch caching.

            For each set of arguments in args_list:
            1. Generate cache keys and check if results are in cache
            2. For cache misses:
               - If batch_fn is provided, call it with all missing arguments at once
               - Otherwise, call the original function for each missing argument set
            3. Update cache with newly computed values
            4. Return results for all argument sets

            Args:
                args_list: List of argument lists to process

            Returns:
                List of results corresponding to each argument list
            """
            keys = [get_key(func, *args) for args in args_list]
            key_to_args = dict(zip(keys, args_list))

            results = cache.get_many(keys)

            missing_keys = [k for k in keys if k not in results]
            missing_args = [key_to_args[k] for k in missing_keys]

            if missing_keys:
                if batch_fn:
                    missing_results = batch_fn(missing_args)
                    missing_values = dict(zip(missing_keys, missing_results))
                else:
                    missing_values = {}
                    for key, args in zip(missing_keys, missing_args):
                        result = func(*args)
                        missing_values[key] = NONE_RESULT if result is None else result

                cache.set_many(missing_values, timeout)
                results.update(missing_values)

            final_results = [results[k] for k in keys]

            return [None if r == NONE_RESULT else r for r in final_results]

        def dirty_multi(args_list):
            keys = [get_key(func, *args) for args in args_list]
            cache.delete_many(keys)

        wrapper.dirty = dirty
        wrapper.batch = batch
        wrapper.dirty_multi = dirty_multi

        return wrapper

    return decorator


class CacheableModel(models.Model):
    cache_version = 1  # Default version, override in subclasses
    cache_timeout = None  # Cache timeout in seconds (default: None)

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cls = self.__class__
        if not hasattr(cls, "_field_names_cache"):
            # Cache all field names
            field_names = {field.name for field in cls._meta.fields}

            # Cache which fields have getter methods
            cls._fields_with_getters = {}
            for field_name in field_names:
                getter_name = f"get_{field_name}"
                cls._fields_with_getters[field_name] = hasattr(cls, getter_name)

    @classmethod
    def get_cached_dict(self, id):
        """
        Override this method to define what data should be cached.
        Return a dictionary.
        """
        raise NotImplementedError("Subclasses must implement get_cached_dict()")

    @classmethod
    def dirty_cache(cls, *ids):
        raise NotImplementedError("Subclasses must implement dirty_cache()")

    def get_cached_value(self, key, default_value=None):
        """Get a value from the cached dictionary."""
        if not hasattr(self, "_cached_dict") or self._cached_dict is None:
            self._cached_dict = self.__class__.get_cached_dict(self.pk)
        return self._cached_dict.get(key, default_value)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.dirty_cache(self.id)

    def delete(self, *args, **kwargs):
        self.dirty_cache(self.id)
        super().delete(*args, **kwargs)

    def __getattribute__(self, name):
        # Avoid recursion for certain attributes
        if name in ("_meta", "_state", "__class__", "__dict__", "id"):
            return super().__getattribute__(name)

        if not self.id:
            return super().__getattribute__(name)

        try:
            # Get class to check for field names cache
            cls = super().__getattribute__("__class__")

            if cls._fields_with_getters.get(name, False):
                is_adding = (
                    super().__getattribute__("_state").adding
                    if hasattr(super().__getattribute__("_state"), "adding")
                    else True
                )
                is_loaded = not is_adding

                # Use getter if:
                # 1. This isn't a database instance (a fresh Model() call)
                # 2. The field isn't loaded or is deferred
                if not is_loaded:
                    getter_method_name = f"get_{name}"
                    if hasattr(
                        super().__getattribute__("__class__"), getter_method_name
                    ):
                        getter_method = super().__getattribute__(getter_method_name)
                        if callable(getter_method):
                            return getter_method()
                elif hasattr(super(), "get_deferred_fields"):
                    deferred_method = super().__getattribute__("get_deferred_fields")
                    is_deferred = name in deferred_method()
                    if is_deferred:
                        getter_method_name = f"get_{name}"
                        if hasattr(
                            super().__getattribute__("__class__"), getter_method_name
                        ):
                            getter_method = super().__getattribute__(getter_method_name)
                            if callable(getter_method):
                                return getter_method()
        except Exception as e:
            pass

        return super().__getattribute__(name)
