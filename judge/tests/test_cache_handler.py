from django.core.cache import cache, caches
from django.test import SimpleTestCase, override_settings

from judge.cache_handler import (
    clear_request_l0_cache,
    start_request_cache_profile,
    stop_request_cache_profile,
)


@override_settings(
    CACHES={
        "default": {"BACKEND": "judge.cache_handler.CacheHandler"},
        "primary": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "request-cache-profiler-tests",
        },
    }
)
class RequestCacheProfilerTest(SimpleTestCase):
    def setUp(self):
        clear_request_l0_cache()
        caches["primary"].clear()

    def tearDown(self):
        stop_request_cache_profile()
        clear_request_l0_cache()
        caches["primary"].clear()

    def test_l0_hits_are_not_counted_as_cache_io(self):
        cache.set("profiled-key", "value")
        clear_request_l0_cache()
        profiler = start_request_cache_profile()

        self.assertEqual(cache.get("profiled-key"), "value")
        self.assertEqual(cache.get("profiled-key"), "value")

        self.assertEqual(profiler.call_count, 1)
        self.assertEqual(profiler.by_operation["get"]["count"], 1)

    def test_fully_l0_get_many_is_not_counted_as_cache_io(self):
        cache.set_many({"first": 1, "second": 2})
        profiler = start_request_cache_profile()

        self.assertEqual(
            cache.get_many(["first", "second"]),
            {"first": 1, "second": 2},
        )

        self.assertEqual(profiler.call_count, 0)
