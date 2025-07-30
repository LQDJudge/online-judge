from django.core.cache.backends.base import BaseCache
from django.core.cache import caches

DEFAULT_L0_TIMEOUT = 60
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
        Retrieve a value from the cache with L0 caching.
        """
        if l0_cache:
            result = l0_cache.get(key, **kwargs)
            if result is not None:
                return result

        result = primary_cache.get(key, **kwargs)
        if result is not None:
            if l0_cache:
                l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
            return result
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
        return primary_cache.add(key, value, timeout, **kwargs)

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

        cache_results = primary_cache.get_many(keys, **kwargs)
        if l0_cache and cache_results:  # Only update L0 if we have results
            l0_cache.set_many(cache_results, DEFAULT_L0_TIMEOUT, **kwargs)
        results.update(cache_results)
        return results

    def set_many(self, data, timeout=None, **kwargs):
        """
        Set multiple values in the cache.
        """
        if l0_cache:
            l0_cache.set_many(data, DEFAULT_L0_TIMEOUT, **kwargs)
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
        result = primary_cache.incr(key, delta, **kwargs)
        if l0_cache:
            l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
        return result

    def decr(self, key, delta=1, **kwargs):
        """
        Decrement a value in the cache.
        """
        result = primary_cache.decr(key, delta, **kwargs)
        if l0_cache:
            l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
        return result
