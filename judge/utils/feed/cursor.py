import base64
import json
from dataclasses import dataclass, asdict


@dataclass
class FeedCursor:
    """
    Tracks consumption offsets across all feed pools.
    Encoded as URL-safe base64 JSON in the ?cursor= query param.
    """

    slot_offset: int = 0
    post_offset: int = 0
    comment_offset: int = 0
    problem_offset: int = 0
    contest_offset: int = 0
    group_card_offset: int = 0

    def encode(self):
        data = json.dumps(asdict(self), separators=(",", ":"))
        return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw):
        if not raw:
            return cls()
        try:
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            return cls(
                **{k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            )
        except Exception:
            return cls()

    def advance(self, **kwargs):
        """Return a new cursor advanced by the given amounts."""
        values = asdict(self)
        for k, v in kwargs.items():
            if k in values:
                values[k] += v
        return FeedCursor(**values)
