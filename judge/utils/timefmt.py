"""Small formatting helpers for human-readable durations.

Kept in `judge/utils/` so any view, task, or template tag can import without
pulling in view-layer deps. Contract: each helper returns a fixed-width
string so client-side regex/templates can splice it without ambiguity.
"""


def format_mmss(seconds) -> str:
    """Format a duration as MM:SS with zero-padded minutes.

    The width is load-bearing: `templates/problem/edit.html` and
    `templates/contest/edit.html` patch the visible countdown via a
    `\\d{2}:\\d{2}` regex. Single-digit minutes would silently fail the swap.
    """
    total = max(0, int(seconds))
    return "%02d:%02d" % (total // 60, total % 60)
