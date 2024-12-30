from django.core.cache.backends.base import BaseCache
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured

NUM_CACHE_RETRY = 3
DEFAULT_L0_TIMEOUT = 300
l0_cache = caches["l0"] if "l0" in caches else None
primary_cache = caches["primary"] if "primary" in caches else None


class CacheHandler(BaseCache):
    """
    Custom Django cache backend with support for L0 (short-term) and primary cache layers.
    """

    def __init__(self, location, params):
        super().__init__(params)

    def get(self, key, default=None, **kwargs):
        """
        Retrieve a value from the cache with retry logic and L0 caching.
        """
        if l0_cache:
            result = l0_cache.get(key, **kwargs)
            if result is not None:
                return result

        for attempt in range(NUM_CACHE_RETRY):
            try:
                result = primary_cache.get(key, **kwargs)
                if result is not None:
                    if l0_cache:
                        l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
                    return result
            except Exception:
                if attempt == NUM_CACHE_RETRY - 1:
                    raise
        return default

    def set(self, key, value, timeout=None, **kwargs):
        """
        Set a value in the cache and optionally in the L0 cache.
        """
        if l0_cache:
            l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
        primary_cache.set(key, value, timeout, **kwargs)

    def delete(self, key, **kwargs):
        """
        Delete a value from both L0 and primary cache.
        """
        if l0_cache:
            l0_cache.delete(key, **kwargs)
        primary_cache.delete(key, **kwargs)

    def add(self, key, value, timeout=None, **kwargs):
        """
        Add a value to the cache only if the key does not already exist.
        """
        if l0_cache and not l0_cache.get(key, **kwargs):
            l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
        primary_cache.add(key, value, timeout, **kwargs)

    def get_many(self, keys, **kwargs):
        """
        Retrieve multiple values from the cache.
        """
        results = {}
        if l0_cache:
            l0_results = l0_cache.get_many(keys, **kwargs)
            results.update(l0_results)
            keys = [key for key in keys if key not in l0_results]

        if not keys:
            return results

        for attempt in range(NUM_CACHE_RETRY):
            try:
                cache_results = primary_cache.get_many(keys, **kwargs)
                if l0_cache:
                    for key, value in cache_results.items():
                        l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
                results.update(cache_results)
                return results
            except Exception:
                if attempt == NUM_CACHE_RETRY - 1:
                    raise
        return results

    def set_many(self, data, timeout=None, **kwargs):
        """
        Set multiple values in the cache.
        """
        if l0_cache:
            for key, value in data.items():
                l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
        primary_cache.set_many(data, timeout, **kwargs)

    def delete_many(self, keys, **kwargs):
        """
        Delete multiple values from the cache.
        """
        if l0_cache:
            l0_cache.delete_many(keys, **kwargs)
        primary_cache.delete_many(keys, **kwargs)

    def clear(self, **kwargs):
        """
        Clear both L0 and primary caches.
        """
        if l0_cache:
            l0_cache.clear(**kwargs)
        primary_cache.clear(**kwargs)

    def incr(self, key, delta=1, **kwargs):
        """
        Increment a value in the cache.
        """
        if l0_cache:
            l0_value = l0_cache.get(key, **kwargs)
            if l0_value is not None:
                updated_value = l0_value + delta
                l0_cache.set(key, updated_value, DEFAULT_L0_TIMEOUT, **kwargs)
                return updated_value
        return primary_cache.incr(key, delta, **kwargs)

    def decr(self, key, delta=1, **kwargs):
        """
        Decrement a value in the cache.
        """
        if l0_cache:
            l0_value = l0_cache.get(key, **kwargs)
            if l0_value is not None:
                updated_value = l0_value - delta
                l0_cache.set(key, updated_value, DEFAULT_L0_TIMEOUT, **kwargs)
                return updated_value
        return primary_cache.decr(key, delta, **kwargs)
