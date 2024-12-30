from django.core.cache import cache
from django.db.models.query import QuerySet
from django.core.handlers.wsgi import WSGIRequest
from django.db import models
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


# Cache decorator
def cache_wrapper(prefix, timeout=None, expected_type=None):
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

        def prefetch_multi(args_list):
            keys = [get_key(func, *args) for args in args_list]
            results = cache.get_many(keys)

            return {
                key: (None if value == NONE_RESULT else value)
                for key, value in results.items()
            }

        def dirty_multi(args_list):
            keys = [get_key(func, *args) for args in args_list]
            cache.delete_many(keys)

        wrapper.dirty = dirty
        wrapper.prefetch_multi = prefetch_multi
        wrapper.dirty_multi = dirty_multi

        return wrapper

    return decorator


# CacheableModel with optimized caching
class CacheableModel(models.Model):
    """
    Base class for models with caching support using cache utilities.
    """

    cache_timeout = None  # Cache timeout in seconds (default: None)

    class Meta:
        abstract = True  # This is an abstract base class and won't create a table

    @classmethod
    def _get_cache_key(cls, obj_id):
        """Generate a cache key based on the model name and object ID."""
        return f"{cls.__name__.lower()}_{obj_id}"

    @classmethod
    def get_instance(cls, *ids):
        """
        Fetch one or multiple objects by IDs using caching.
        """
        if not ids:
            return None

        ids = ids[0] if len(ids) == 1 and isinstance(ids[0], (list, tuple)) else ids
        cache_keys = {cls._get_cache_key(obj_id): obj_id for obj_id in ids}
        cached_objects = cache.get_many(cache_keys.keys())

        # Handle NONE_RESULT logic
        results = {
            cache_keys[key]: (
                None
                if cached_objects[key] == NONE_RESULT
                else cls(**cached_objects[key])
            )
            for key in cached_objects
        }
        missing_ids = [obj_id for obj_id in ids if obj_id not in results]

        if missing_ids:
            missing_objects = cls.objects.filter(id__in=missing_ids)
            objects_to_cache = {}
            for obj in missing_objects:
                obj_dict = model_to_dict(obj)
                cache_key = cls._get_cache_key(obj.id)
                objects_to_cache[cache_key] = obj_dict if obj_dict else NONE_RESULT
                results[obj.id] = cls(**obj_dict)
            cache.set_many(objects_to_cache, timeout=cls.cache_timeout)

        return results[ids[0]] if len(ids) == 1 else [results[obj_id] for obj_id in ids]

    @classmethod
    def dirty_cache(cls, *ids):
        """
        Clear the cache for one or multiple object IDs using delete_many.
        """
        if not ids:
            return

        ids = ids[0] if len(ids) == 1 and isinstance(ids[0], (list, tuple)) else ids
        cache_keys = [cls._get_cache_key(obj_id) for obj_id in ids]
        cache.delete_many(cache_keys)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.dirty_cache(self.id)

    def delete(self, *args, **kwargs):
        self.dirty_cache(self.id)
        super().delete(*args, **kwargs)
