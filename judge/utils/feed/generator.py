"""
Feed generator: fills a slot template from data source pools.

The slot template repeats infinitely. Each page is a window [slot_offset, slot_offset + page_size)
into this infinite cycle. Only pools referenced by slots in the current window are fetched.
"""

from .cursor import FeedCursor
from .pools import PostPool, CommentPool, ProblemPool, ContestPool, GroupCardPool

# Slot types
POST = "post"
COMMENT = "comment"
PROBLEM = "problem"
CONTEST = "contest"
GROUP_CARD = "group_card"

# The repeating slot pattern
SLOT_TEMPLATE = [
    POST,
    POST,
    POST,
    COMMENT,
    PROBLEM,
    COMMENT,
    POST,
    CONTEST,
    COMMENT,
    PROBLEM,
    GROUP_CARD,
    COMMENT,
]

TEMPLATE_LEN = len(SLOT_TEMPLATE)

# Maps slot type -> (pool attribute name, cursor field name)
SLOT_POOL_MAP = {
    POST: ("post_pool", "post_offset"),
    COMMENT: ("comment_pool", "comment_offset"),
    PROBLEM: ("problem_pool", "problem_offset"),
    CONTEST: ("contest_pool", "contest_offset"),
    GROUP_CARD: ("group_pool", "group_card_offset"),
}


class FeedGenerator:
    """
    Assembles a feed page by filling slots from the template cycle.
    """

    def __init__(self, request, organization=None):
        self.post_pool = PostPool(request, organization)
        self.comment_pool = CommentPool(request, organization)
        self.problem_pool = ProblemPool(request, organization)
        self.contest_pool = ContestPool(request, organization)
        self.group_pool = GroupCardPool(request, organization)

    def generate_page(self, cursor, page_size):
        """
        Generate one page of feed items.

        Takes slots [cursor.slot_offset, cursor.slot_offset + page_size) from the
        infinite repeating template. Only fetches from pools that appear in those slots.

        Returns:
            (items, next_cursor) — next_cursor is None if no more content.
        """
        items = []
        consumed = {
            field: 0
            for field in FeedCursor.__dataclass_fields__
            if field != "slot_offset"
        }
        slots_advanced = 0

        # Keep filling until we have enough items or exhaust a full cycle with no new items
        max_slots = page_size + TEMPLATE_LEN  # prevent infinite loop
        while len(items) < page_size and slots_advanced < max_slots:
            slot_idx = (cursor.slot_offset + slots_advanced) % TEMPLATE_LEN
            slot_type = SLOT_TEMPLATE[slot_idx]

            pool_attr, cursor_field = SLOT_POOL_MAP[slot_type]
            pool = getattr(self, pool_attr)
            offset = getattr(cursor, cursor_field) + consumed[cursor_field]

            batch = pool.get(offset, 1)
            if batch:
                items.append(batch[0])
                consumed[cursor_field] += 1

            slots_advanced += 1

        # Feed ends when no pool has more content
        has_more = any(
            getattr(self, pool_attr).has_more(
                getattr(cursor, cursor_field) + consumed[cursor_field]
            )
            for pool_attr, cursor_field in SLOT_POOL_MAP.values()
        )

        if has_more:
            new_slot_offset = (cursor.slot_offset + slots_advanced) % TEMPLATE_LEN
            # Override slot_offset instead of advancing it
            consumed_copy = dict(consumed)
            next_cursor = FeedCursor(
                slot_offset=new_slot_offset,
                **{k: getattr(cursor, k) + v for k, v in consumed_copy.items()},
            )
        else:
            next_cursor = None

        return items, next_cursor
