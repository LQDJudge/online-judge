"""
Homepage mixed feed.

Public API:
    build_home_feed(request, cursor_str, feed_type, sort_by) -> dict
"""

from .cursor import FeedCursor
from .generator import FeedGenerator
from .items import FeedItem


FIRST_PAGE_SIZE = 4
SCROLL_PAGE_SIZE = 6


def build_home_feed(request, cursor_str=None, organization=None):
    """
    Build one page of the homepage mixed feed.

    Args:
        request: Django request
        cursor_str: encoded cursor from ?cursor= param
        organization: Organization to scope feed to (None for global homepage)

    For logged-out users, returns None (view should fall back to simple posts).

    Returns dict with:
        'items': list of FeedItem
        'next_cursor': encoded cursor string or None
        'has_next_page': bool
    """
    if not request.user.is_authenticated:
        return None

    cursor = FeedCursor.decode(cursor_str)
    generator = FeedGenerator(request, organization=organization)

    # First page shows 4 items; subsequent pages show 6
    page_size = FIRST_PAGE_SIZE if not cursor_str else SCROLL_PAGE_SIZE
    items, next_cursor = generator.generate_page(cursor, page_size)

    return {
        "items": items,
        "next_cursor": next_cursor.encode() if next_cursor else None,
        "has_next_page": next_cursor is not None,
    }
