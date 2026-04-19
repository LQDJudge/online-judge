# Docker-specific settings — mounted over local_settings.py inside the container.
# Uses the real mysqlclient driver (installed via libmysqlclient-dev), no pymysql shim needed.

SECRET_KEY = "lqdoj-docker-dev-key-not-for-production"

DEBUG = True
ALLOWED_HOSTS = ["*"]

# ── Database ─────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": "dmoj",
        "USER": "dmoj",
        "PASSWORD": "dmoj",
        "HOST": "db",
        "PORT": "3306",
        "OPTIONS": {
            "charset": "utf8mb4",
            "sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
        },
    }
}

# ── Cache (memcached) ────────────────────────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "judge.cache_handler.CacheHandler",
    },
    "primary": {
        "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
        "LOCATION": "memcached:11211",
    },
}

# ── Celery / Redis ───────────────────────────────────────────────────────────
CELERY_BROKER_URL_SECRET = "redis://redis:6379"
CELERY_RESULT_BACKEND_SECRET = "redis://redis:6379"

# ── Static / Media / Problems ────────────────────────────────────────────────
STATIC_ROOT = "/static"
MEDIA_ROOT = "/media"
DMOJ_PROBLEM_DATA_ROOT = "/problems"

# ── Site identity ────────────────────────────────────────────────────────────
SITE_NAME = "LQDOJ"
SITE_LONG_NAME = "Le Quy Don Online Judge"

# ── Chat (required) ──────────────────────────────────────────────────────────
CHAT_SECRET_KEY = "ey7AAB1E6_14AMGUNY6yIKM05wWx9dTE9N0naq4Hr58="

# ── Event daemon (disabled for basic dev) ───────────────────────────────────
EVENT_DAEMON_SUBMISSION_KEY = "docker-dev-submission-key"

# ── Bridge (judge ↔ Django) ──────────────────────────────────────────────────
# Bridge listens on 0.0.0.0 so both web and judge containers can reach it.
# Web container connects to bridge by Docker Compose service name.
BRIDGED_JUDGE_ADDRESS = [("0.0.0.0", 9999)]  # judges connect here
BRIDGED_DJANGO_ADDRESS = [("0.0.0.0", 9998)]  # web connects here
BRIDGED_DJANGO_CONNECT = (
    "bridge",
    9998,
)  # web → bridge hostname (must be a single tuple)
