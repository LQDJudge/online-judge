"""Explicit consumer-specific streaming policies, independent of POE_TIMEOUT."""

from django.conf import settings

# (first meaningful output, subsequent silence, entire call), in seconds.
# Features with up to three application attempts stay below the 600s task limit.
FEATURE_TIMEOUTS = {
    "problem_tagging": (90, 60, 150),
    "problem_markdown": (90, 60, 150),
    "solution_generation": (120, 90, 180),
    "problem_ai": (120, 90, 240),
    "quiz_ai": (120, 90, 240),
    "quiz_import": (180, 120, 300),
    "solution_code_generation": (180, 120, 480),
    "problem_chatbot": (120, 90, 300),
}


def streaming_options(feature):
    first, idle, total = FEATURE_TIMEOUTS[feature]
    overrides = getattr(settings, "AI_FEATURE_STREAMING_TIMEOUTS", {}).get(feature, {})
    limits = {
        "connect": 20,
        "first_output": first,
        "idle": idle,
        "total": total,
        **overrides,
    }
    if limits["total"] + 60 > getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 600):
        raise ValueError("AI streaming policy exceeds task execution budget")
    # Existing consumer return contracts stay intact; failures are available via
    # last_error and usage metadata rather than unexpectedly raising to users.
    return {
        "streaming_timeouts": limits,
        "transport_attempts": 1,
        "timeout": limits["total"],
    }
