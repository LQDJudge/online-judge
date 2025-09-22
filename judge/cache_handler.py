from django.core.cache.backends.base import BaseCache
from django.core.cache import caches
from django.conf import settings
import threading
import time
import sys

DEFAULT_L0_TIMEOUT = 60
primary_cache = caches["primary"] if "primary" in caches else None

# Thread-local storage for request-scoped L0 cache
_thread_local = threading.local()


class L0CacheStats:
    """Statistics collection for L0 and primary cache operations."""

    def __init__(self):
        # L0 cache statistics (existing)
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.deletes = 0
        self.evictions = 0
        self.total_get_time = 0.0
        self.total_set_time = 0.0

        # Primary cache statistics (new)
        self.primary_hits = 0
        self.primary_misses = 0
        self.primary_sets = 0
        self.primary_deletes = 0
        self.primary_get_time = 0.0
        self.primary_set_time = 0.0
        self.primary_errors = 0

        self.start_time = time.perf_counter()

    def record_hit(self, duration=0.0):
        """Record an L0 cache hit."""
        self.hits += 1
        self.total_get_time += duration

    def record_miss(self, duration=0.0):
        """Record an L0 cache miss."""
        self.misses += 1
        self.total_get_time += duration

    def record_set(self, duration=0.0):
        """Record an L0 cache set operation."""
        self.sets += 1
        self.total_set_time += duration

    def record_delete(self):
        """Record an L0 cache delete operation."""
        self.deletes += 1

    def record_eviction(self):
        """Record an L0 cache eviction."""
        self.evictions += 1

    def record_primary_hit(self, duration=0.0):
        """Record a primary cache hit."""
        self.primary_hits += 1
        self.primary_get_time += duration

    def record_primary_miss(self, duration=0.0):
        """Record a primary cache miss."""
        self.primary_misses += 1
        self.primary_get_time += duration

    def record_primary_set(self, duration=0.0):
        """Record a primary cache set operation."""
        self.primary_sets += 1
        self.primary_set_time += duration

    def record_primary_delete(self):
        """Record a primary cache delete operation."""
        self.primary_deletes += 1

    def record_primary_error(self):
        """Record a primary cache error."""
        self.primary_errors += 1

    @property
    def hit_ratio(self):
        """Calculate L0 cache hit ratio."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    @property
    def primary_hit_ratio(self):
        """Calculate primary cache hit ratio."""
        total = self.primary_hits + self.primary_misses
        return self.primary_hits / total if total > 0 else 0.0

    @property
    def overall_hit_ratio(self):
        """Calculate overall hit ratio (L0 + primary)."""
        total_hits = self.hits + self.primary_hits
        total_requests = (
            self.hits + self.misses + self.primary_hits + self.primary_misses
        )
        return total_hits / total_requests if total_requests > 0 else 0.0

    @property
    def cache_efficiency_ratio(self):
        """Calculate cache efficiency ratio (L0 hits vs total hits)."""
        total_hits = self.hits + self.primary_hits
        return self.hits / total_hits if total_hits > 0 else 0.0

    @property
    def avg_get_time(self):
        """Calculate average L0 get time."""
        total_gets = self.hits + self.misses
        return self.total_get_time / total_gets if total_gets > 0 else 0.0

    @property
    def avg_set_time(self):
        """Calculate average L0 set time."""
        return self.total_set_time / self.sets if self.sets > 0 else 0.0

    @property
    def primary_avg_get_time(self):
        """Calculate average primary cache get time."""
        total_gets = self.primary_hits + self.primary_misses
        return self.primary_get_time / total_gets if total_gets > 0 else 0.0

    @property
    def primary_avg_set_time(self):
        """Calculate average primary cache set time."""
        return (
            self.primary_set_time / self.primary_sets if self.primary_sets > 0 else 0.0
        )

    @property
    def uptime(self):
        """Calculate cache uptime."""
        return time.perf_counter() - self.start_time

    def get_summary(self):
        """Get a summary of L0 cache statistics (backward compatible)."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "deletes": self.deletes,
            "evictions": self.evictions,
            "hit_ratio": self.hit_ratio,
            "avg_get_time_ms": self.avg_get_time * 1000,
            "avg_set_time_ms": self.avg_set_time * 1000,
            "uptime_seconds": self.uptime,
        }

    def get_comprehensive_summary(self):
        """Get a comprehensive summary of both L0 and primary cache statistics."""
        return {
            # L0 cache statistics
            "l0_hits": self.hits,
            "l0_misses": self.misses,
            "l0_sets": self.sets,
            "l0_deletes": self.deletes,
            "l0_evictions": self.evictions,
            "l0_hit_ratio": self.hit_ratio,
            "l0_avg_get_time_ms": self.avg_get_time * 1000,
            "l0_avg_set_time_ms": self.avg_set_time * 1000,
            # Primary cache statistics
            "primary_hits": self.primary_hits,
            "primary_misses": self.primary_misses,
            "primary_sets": self.primary_sets,
            "primary_deletes": self.primary_deletes,
            "primary_errors": self.primary_errors,
            "primary_hit_ratio": self.primary_hit_ratio,
            "primary_avg_get_time_ms": self.primary_avg_get_time * 1000,
            "primary_avg_set_time_ms": self.primary_avg_set_time * 1000,
            # Combined statistics
            "overall_hit_ratio": self.overall_hit_ratio,
            "cache_efficiency_ratio": self.cache_efficiency_ratio,
            "uptime_seconds": self.uptime,
        }


class L0Cache:
    """Enhanced L0 cache with size limiting and LRU eviction."""

    def __init__(self, max_entries=None, max_memory_mb=None, debug=False):
        self.max_entries = max_entries
        self.max_memory_mb = max_memory_mb
        self.debug = debug

        # Core storage
        self._data = {}
        self._access_counter = 0
        self._access_times = {}  # key -> access_counter for LRU

        # Statistics (only if debug enabled)
        self.stats = L0CacheStats() if debug else None

    def _estimate_memory_usage(self):
        """Estimate memory usage of the cache in MB."""
        if not self._data:
            return 0.0

        # Rough estimation: sys.getsizeof for keys and values
        total_size = 0
        for key, value in self._data.items():
            total_size += sys.getsizeof(key) + sys.getsizeof(value)

        return total_size / (1024 * 1024)  # Convert to MB

    def _should_evict(self):
        """Check if eviction is needed based on size or memory limits."""
        if self.max_entries and len(self._data) >= self.max_entries:
            return True

        if self.max_memory_mb and self._estimate_memory_usage() >= self.max_memory_mb:
            return True

        return False

    def _evict_lru(self):
        """Evict the least recently used item."""
        if not self._data:
            return

        # Find the key with the smallest access counter
        lru_key = min(self._access_times.keys(), key=lambda k: self._access_times[k])

        # Remove from both data and access tracking
        del self._data[lru_key]
        del self._access_times[lru_key]

        if self.stats:
            self.stats.record_eviction()

    def get(self, key, default=None):
        """Get a value from the cache."""
        start_time = time.perf_counter() if self.debug else 0

        if key in self._data:
            # Update access time for LRU
            self._access_counter += 1
            self._access_times[key] = self._access_counter

            if self.stats:
                duration = time.perf_counter() - start_time
                self.stats.record_hit(duration)

            return self._data[key]

        if self.stats:
            duration = time.perf_counter() - start_time
            self.stats.record_miss(duration)

        return default

    def set(self, key, value):
        """Set a value in the cache."""
        start_time = time.perf_counter() if self.debug else 0

        # Check if we need to evict before adding
        if key not in self._data and self._should_evict():
            self._evict_lru()

        # Set the value and update access tracking
        self._data[key] = value
        self._access_counter += 1
        self._access_times[key] = self._access_counter

        if self.stats:
            duration = time.perf_counter() - start_time
            self.stats.record_set(duration)

    def delete(self, key):
        """Delete a value from the cache."""
        if key in self._data:
            del self._data[key]
            del self._access_times[key]

            if self.stats:
                self.stats.record_delete()

    def clear(self):
        """Clear all values from the cache."""
        self._data.clear()
        self._access_times.clear()
        self._access_counter = 0

    def update(self, data):
        """Update the cache with multiple key-value pairs."""
        for key, value in data.items():
            self.set(key, value)

    def pop(self, key, default=None):
        """Remove and return a value from the cache."""
        if key in self._data:
            value = self._data[key]
            self.delete(key)
            return value
        return default

    def __contains__(self, key):
        """Check if a key exists in the cache."""
        return key in self._data

    def __getitem__(self, key):
        """Get item using bracket notation."""
        result = self.get(key)
        if result is None and key not in self._data:
            raise KeyError(key)
        return result

    def __setitem__(self, key, value):
        """Set item using bracket notation."""
        self.set(key, value)

    def __len__(self):
        """Get the number of items in the cache."""
        return len(self._data)

    def keys(self):
        """Get cache keys."""
        return self._data.keys()

    def values(self):
        """Get cache values."""
        return self._data.values()

    def items(self):
        """Get cache items."""
        return self._data.items()


def _get_cache_stats_config():
    """Get cache statistics configuration with backward compatibility."""
    # Check for new CACHE_STATS_CONFIG setting first
    if hasattr(settings, "CACHE_STATS_CONFIG"):
        config = settings.CACHE_STATS_CONFIG
        return {
            "enabled": config.get("enabled", False),
            "track_primary": config.get("track_primary", True),
            "detailed_logging": config.get("detailed_logging", False),
        }

    return {
        "enabled": False,
        "track_primary": False,  # Enable primary tracking if debug is on
        "detailed_logging": False,  # Keep existing behavior
    }


def _get_l0_cache_config():
    """Get L0 cache configuration from Django settings."""
    stats_config = _get_cache_stats_config()
    return {
        "max_entries": getattr(settings, "L0_CACHE_MAX_ENTRIES", 1000),
        "max_memory_mb": getattr(settings, "L0_CACHE_MAX_MEMORY_MB", None),
        "debug": stats_config["enabled"],
    }


def get_request_l0_cache():
    """Get or create a request-scoped L0 cache."""
    if not hasattr(_thread_local, "l0_cache"):
        config = _get_l0_cache_config()
        _thread_local.l0_cache = L0Cache(**config)
    return _thread_local.l0_cache


def clear_request_l0_cache():
    """Clear the request-scoped L0 cache."""
    if hasattr(_thread_local, "l0_cache"):
        cache = _thread_local.l0_cache

        # Log statistics if debug is enabled
        if cache.debug and cache.stats:
            stats_config = _get_cache_stats_config()

            if stats_config["detailed_logging"]:
                # Use comprehensive summary for detailed logging
                stats = cache.stats.get_comprehensive_summary()
                print(f"Cache Stats (Comprehensive): {stats}")
            else:
                # Use backward compatible summary for existing behavior
                stats = cache.stats.get_summary()
                print(f"L0 Cache Stats: {stats}")

                # Also log primary cache stats if tracking is enabled and there's activity
                if stats_config["track_primary"] and (
                    cache.stats.primary_hits > 0
                    or cache.stats.primary_misses > 0
                    or cache.stats.primary_sets > 0
                    or cache.stats.primary_errors > 0
                ):
                    primary_stats = {
                        "primary_hits": cache.stats.primary_hits,
                        "primary_misses": cache.stats.primary_misses,
                        "primary_sets": cache.stats.primary_sets,
                        "primary_deletes": cache.stats.primary_deletes,
                        "primary_errors": cache.stats.primary_errors,
                        "primary_hit_ratio": cache.stats.primary_hit_ratio,
                        "primary_avg_get_time_ms": cache.stats.primary_avg_get_time
                        * 1000,
                        "primary_avg_set_time_ms": cache.stats.primary_avg_set_time
                        * 1000,
                    }
                    print(f"Primary Cache Stats: {primary_stats}")

        cache.clear()


class CacheHandler(BaseCache):
    """
    Custom Django cache backend with support for request-scoped L0 (short-term) and primary cache layers.
    """

    def __init__(self, location, params):
        super().__init__(params)

    def get(self, key, default=None, **kwargs):
        """
        Retrieve a value from the cache with request-scoped L0 caching.
        """
        l0_cache = get_request_l0_cache()
        result = l0_cache.get(key)
        if result is not None:
            return result

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                result = primary_cache.get(key, **kwargs)
                duration = time.perf_counter() - start_time

                if result is not None:
                    l0_cache.stats.record_primary_hit(duration)
                    l0_cache.set(key, result)
                    return result
                else:
                    l0_cache.stats.record_primary_miss(duration)
                    return default
            except Exception as e:
                l0_cache.stats.record_primary_error()
                return default
        else:
            # Original behavior when stats are disabled
            result = primary_cache.get(key, **kwargs)
            if result is not None:
                l0_cache.set(key, result)
                return result
            return default

    def set(self, key, value, timeout=None, **kwargs):
        """
        Set a value in the cache and in the request-scoped L0 cache.
        """
        l0_cache = get_request_l0_cache()
        l0_cache.set(key, value)

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                primary_cache.set(key, value, timeout, **kwargs)
                duration = time.perf_counter() - start_time
                l0_cache.stats.record_primary_set(duration)
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise  # Re-raise since set operations should fail if primary cache fails
        else:
            # Original behavior when stats are disabled
            primary_cache.set(key, value, timeout, **kwargs)

    def delete(self, key, **kwargs):
        """
        Delete a value from both request-scoped L0 and primary cache.
        """
        l0_cache = get_request_l0_cache()
        l0_cache.delete(key)

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            try:
                primary_cache.delete(key, **kwargs)
                l0_cache.stats.record_primary_delete()
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise  # Re-raise since delete operations should fail if primary cache fails
        else:
            # Original behavior when stats are disabled
            primary_cache.delete(key, **kwargs)

    def add(self, key, value, timeout=None, **kwargs):
        """
        Add a value to the cache only if the key does not already exist.
        """
        l0_cache = get_request_l0_cache()
        if key not in l0_cache:
            l0_cache.set(key, value)

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                result = primary_cache.add(key, value, timeout, **kwargs)
                duration = time.perf_counter() - start_time
                l0_cache.stats.record_primary_set(duration)
                return result
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise  # Re-raise since add operations should fail if primary cache fails
        else:
            # Original behavior when stats are disabled
            return primary_cache.add(key, value, timeout, **kwargs)

    def get_many(self, keys, **kwargs):
        """
        Retrieve multiple values from the cache with request-scoped L0 caching.
        """
        l0_cache = get_request_l0_cache()
        results = {}

        # Get values from L0 cache first
        l0_results = {}
        for key in keys:
            value = l0_cache.get(key)
            if value is not None:
                l0_results[key] = value
        results.update(l0_results)

        # Get remaining keys from primary cache
        remaining_keys = [key for key in keys if key not in l0_results]
        if not remaining_keys:
            return results

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                cache_results = primary_cache.get_many(remaining_keys, **kwargs)
                duration = time.perf_counter() - start_time

                # Record hits and misses for each key
                for key in remaining_keys:
                    if key in cache_results:
                        l0_cache.stats.record_primary_hit(
                            duration / len(remaining_keys)
                        )
                    else:
                        l0_cache.stats.record_primary_miss(
                            duration / len(remaining_keys)
                        )

                if cache_results:
                    # Update L0 cache with results from primary cache
                    for key, value in cache_results.items():
                        l0_cache.set(key, value)
                results.update(cache_results)
                return results
            except Exception as e:
                l0_cache.stats.record_primary_error()
                return results
        else:
            # Original behavior when stats are disabled
            cache_results = primary_cache.get_many(remaining_keys, **kwargs)
            if cache_results:
                # Update L0 cache with results from primary cache
                for key, value in cache_results.items():
                    l0_cache.set(key, value)
            results.update(cache_results)
            return results

    def set_many(self, data, timeout=None, **kwargs):
        """
        Set multiple values in the cache and request-scoped L0 cache.
        """
        l0_cache = get_request_l0_cache()
        for key, value in data.items():
            l0_cache.set(key, value)

        # Track primary cache operations if stats are enabled
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                primary_cache.set_many(data, timeout, **kwargs)
                duration = time.perf_counter() - start_time
                for _ in data:
                    l0_cache.stats.record_primary_set(duration / len(data))
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise
        else:
            primary_cache.set_many(data, timeout, **kwargs)

    def delete_many(self, keys, **kwargs):
        """
        Delete multiple values from both request-scoped L0 and primary cache.
        """
        l0_cache = get_request_l0_cache()
        for key in keys:
            l0_cache.delete(key)

        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            try:
                primary_cache.delete_many(keys, **kwargs)
                for _ in keys:
                    l0_cache.stats.record_primary_delete()
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise
        else:
            primary_cache.delete_many(keys, **kwargs)

    def clear(self, **kwargs):
        """
        Clear both request-scoped L0 and primary caches.
        """
        clear_request_l0_cache()

        l0_cache = get_request_l0_cache()
        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            try:
                primary_cache.clear(**kwargs)
                l0_cache.stats.record_primary_delete()
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise
        else:
            primary_cache.clear(**kwargs)

    def incr(self, key, delta=1, **kwargs):
        """
        Increment a value in the cache and update request-scoped L0 cache.
        """
        l0_cache = get_request_l0_cache()

        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                result = primary_cache.incr(key, delta, **kwargs)
                duration = time.perf_counter() - start_time
                l0_cache.stats.record_primary_set(
                    duration
                )  # Treat incr as a set operation
                l0_cache.set(key, result)
                return result
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise
        else:
            result = primary_cache.incr(key, delta, **kwargs)
            l0_cache.set(key, result)
            return result

    def decr(self, key, delta=1, **kwargs):
        """
        Decrement a value in the cache and update request-scoped L0 cache.
        """
        l0_cache = get_request_l0_cache()

        stats_config = _get_cache_stats_config()
        if stats_config["track_primary"] and l0_cache.stats:
            start_time = time.perf_counter()
            try:
                result = primary_cache.decr(key, delta, **kwargs)
                duration = time.perf_counter() - start_time
                l0_cache.stats.record_primary_set(
                    duration
                )  # Treat decr as a set operation
                l0_cache.set(key, result)
                return result
            except Exception as e:
                l0_cache.stats.record_primary_error()
                raise
        else:
            result = primary_cache.decr(key, delta, **kwargs)
            l0_cache.set(key, result)
            return result
