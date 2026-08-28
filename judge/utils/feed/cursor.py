import base64
import json
from dataclasses import asdict, dataclass, field


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
    seen_content_keys: list = field(default_factory=list)

    def encode(self):
        values = asdict(self)
        values["seen_content_keys"] = [list(key) for key in self.seen_content_keys]
        data = json.dumps(values, separators=(",", ":"))
        return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw):
        if not raw:
            return cls()
        try:
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            values = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            values["seen_content_keys"] = [
                tuple(key) for key in values.get("seen_content_keys", [])
            ]
            return cls(**values)
        except Exception:
            return cls()

    def advance(self, **kwargs):
        """Return a new cursor advanced by the given amounts."""
        values = asdict(self)
        for k, v in kwargs.items():
            if k in values:
                values[k] += v
        return FeedCursor(**values)
