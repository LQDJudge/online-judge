from django.core.cache.backends.base import BaseCache
from django.core.cache import caches

DEFAULT_L0_TIMEOUT = 60


class CacheHandler(BaseCache):
    """
    Custom Django cache backend with support for L0 (short-term) and primary cache layers.
    """

    def __init__(self, location, params):
        super().__init__(params)
        self.l0_cache = caches["l0"] if "l0" in caches else None
        self.primary_cache = caches["primary"] if "primary" in caches else None

    def get(self, key, default=None, **kwargs):
        """
        Retrieve a value from the cache with L0 caching.
        """
        if self.l0_cache:
            result = self.l0_cache.get(key, **kwargs)
            if result is not None:
                return result

        result = self.primary_cache.get(key, **kwargs)
        if result is not None:
            if self.l0_cache:
                self.l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
            return result
        return default

    def set(self, key, value, timeout=None, **kwargs):
        """
        Set a value in the cache and optionally in the L0 cache.
        """
        if self.l0_cache:
            self.l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
        self.primary_cache.set(key, value, timeout, **kwargs)

    def delete(self, key, **kwargs):
        """
        Delete a value from both L0 and primary cache.
        """
        if self.l0_cache:
            self.l0_cache.delete(key, **kwargs)
        self.primary_cache.delete(key, **kwargs)

    def add(self, key, value, timeout=None, **kwargs):
        """
        Add a value to the cache only if the key does not already exist.
        """
        if self.l0_cache and not self.l0_cache.get(key, **kwargs):
            self.l0_cache.set(key, value, DEFAULT_L0_TIMEOUT, **kwargs)
        return self.primary_cache.add(key, value, timeout, **kwargs)

    def get_many(self, keys, **kwargs):
        """
        Retrieve multiple values from the cache.
        """
        results = {}
        if self.l0_cache:
            l0_results = self.l0_cache.get_many(keys, **kwargs)
            results.update(l0_results)
            keys = [key for key in keys if key not in l0_results]

        if not keys:
            return results

        cache_results = self.primary_cache.get_many(keys, **kwargs)
        if self.l0_cache and cache_results:  # Only update L0 if we have results
            self.l0_cache.set_many(cache_results, DEFAULT_L0_TIMEOUT, **kwargs)
        results.update(cache_results)
        return results

    def set_many(self, data, timeout=None, **kwargs):
        """
        Set multiple values in the cache.
        """
        if self.l0_cache:
            self.l0_cache.set_many(data, DEFAULT_L0_TIMEOUT, **kwargs)
        self.primary_cache.set_many(data, timeout, **kwargs)

    def delete_many(self, keys, **kwargs):
        """
        Delete multiple values from the cache.
        """
        if self.l0_cache:
            self.l0_cache.delete_many(keys, **kwargs)
        self.primary_cache.delete_many(keys, **kwargs)

    def clear(self, **kwargs):
        """
        Clear both L0 and primary caches.
        """
        if self.l0_cache:
            self.l0_cache.clear(**kwargs)
        self.primary_cache.clear(**kwargs)

    def incr(self, key, delta=1, **kwargs):
        """
        Increment a value in the cache.
        """
        result = self.primary_cache.incr(key, delta, **kwargs)
        if self.l0_cache:
            self.l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
        return result

    def decr(self, key, delta=1, **kwargs):
        """
        Decrement a value in the cache.
        """
        result = self.primary_cache.decr(key, delta, **kwargs)
        if self.l0_cache:
            self.l0_cache.set(key, result, DEFAULT_L0_TIMEOUT, **kwargs)
        return result
