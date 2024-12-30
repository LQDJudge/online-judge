from django.core.cache.backends.base import BaseCache
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured

NUM_CACHE_RETRY = 3
DEFAULT_L0_TIMEOUT = 300
NONE_RESULT = "__None__"
l0_cache = caches["l0"] if "l0" in caches else None
primary_cache = caches["primary"] if "primary" in caches else None


class CacheHandler(BaseCache):
    """
    Custom Django cache backend with support for L0 (short-term) and primary cache layers.
    """

    def __init__(self, location, params):
        """
        Initialize the cache backend with L0 and primary (default) cache.
        """
        super().__init__(params)

    def get(self, key, default=None):
        """
        Retrieve a value from the cache with retry logic and L0 caching.
        """
        if l0_cache:
            result = l0_cache.get(key)
            if result is not None:
                return None if result == NONE_RESULT else result

        for attempt in range(NUM_CACHE_RETRY):
            try:
                result = primary_cache.get(key)
                if result is not None:
                    if l0_cache:
                        l0_cache.set(
                            key,
                            NONE_RESULT if result is None else result,
                            DEFAULT_L0_TIMEOUT,
                        )  # Cache in L0
                    return None if result == NONE_RESULT else result
            except Exception:
                if attempt == NUM_CACHE_RETRY - 1:
                    raise
        return default

    def set(self, key, value, timeout=None):
        """
        Set a value in the cache and optionally in the L0 cache.
        """
        value_to_store = NONE_RESULT if value is None else value
        if l0_cache:
            l0_cache.set(key, value_to_store, DEFAULT_L0_TIMEOUT)
        primary_cache.set(key, value_to_store, timeout)

    def delete(self, key):
        """
        Delete a value from both L0 and primary cache.
        """
        if l0_cache:
            l0_cache.delete(key)
        primary_cache.delete(key)

    def add(self, key, value, timeout=None):
        """
        Add a value to the cache only if the key does not already exist.
        """
        value_to_store = NONE_RESULT if value is None else value
        if l0_cache and not l0_cache.get(key):
            l0_cache.set(key, value_to_store, DEFAULT_L0_TIMEOUT)
        primary_cache.add(key, value_to_store, timeout)

    def get_many(self, keys):
        """
        Retrieve multiple values from the cache.
        """
        results = {}
        if l0_cache:
            l0_results = l0_cache.get_many(keys)
            results.update(
                {
                    key: (None if value == NONE_RESULT else value)
                    for key, value in l0_results.items()
                }
            )
            keys = [key for key in keys if key not in l0_results]

        if not keys:
            return results

        for attempt in range(NUM_CACHE_RETRY):
            try:
                cache_results = primary_cache.get_many(keys)
                if l0_cache:
                    for key, value in cache_results.items():
                        l0_cache.set(
                            key,
                            NONE_RESULT if value is None else value,
                            DEFAULT_L0_TIMEOUT,
                        )
                results.update(
                    {
                        key: (None if value == NONE_RESULT else value)
                        for key, value in cache_results.items()
                    }
                )
                return results
            except Exception:
                if attempt == NUM_CACHE_RETRY - 1:
                    raise
        return results

    def set_many(self, data, timeout=None):
        """
        Set multiple values in the cache.
        """
        data_to_store = {
            key: (NONE_RESULT if value is None else value)
            for key, value in data.items()
        }
        if l0_cache:
            for key, value in data_to_store.items():
                l0_cache.set(key, value, DEFAULT_L0_TIMEOUT)
        primary_cache.set_many(data_to_store, timeout)

    def delete_many(self, keys):
        """
        Delete multiple values from the cache.
        """
        if l0_cache:
            l0_cache.delete_many(keys)
        primary_cache.delete_many(keys)

    def clear(self):
        """
        Clear both L0 and primary caches.
        """
        if l0_cache:
            l0_cache.clear()
        primary_cache.clear()

    def incr(self, key, delta=1):
        """
        Increment a value in the cache.
        """
        if l0_cache:
            l0_value = l0_cache.get(key)
            if l0_value and l0_value != NONE_RESULT:
                updated_value = l0_value + delta
                l0_cache.set(key, updated_value, DEFAULT_L0_TIMEOUT)
                return updated_value
        return primary_cache.incr(key, delta)

    def decr(self, key, delta=1):
        """
        Decrement a value in the cache.
        """
        if l0_cache:
            l0_value = l0_cache.get(key)
            if l0_value and l0_value != NONE_RESULT:
                updated_value = l0_value - delta
                l0_cache.set(key, updated_value, DEFAULT_L0_TIMEOUT)
                return updated_value
        return primary_cache.decr(key, delta)
