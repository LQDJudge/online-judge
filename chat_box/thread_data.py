"""Batched thread metadata shared by page rendering and the JSON endpoints."""

from django.db.models import (
    BigIntegerField,
    Count,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce

from chat_box.models import Message, ThreadParticipation


def thread_summaries(roots, profile):
    root_ids = [root.id for root in roots if not root.thread_root_id]
    if not root_ids:
        return {}
    participations = {
        row.root_id: row
        for row in ThreadParticipation.objects.filter(
            root_id__in=root_ids, user=profile
        )
    }
    cursor = ThreadParticipation.objects.filter(
        root_id=OuterRef("thread_root_id"), user=profile
    ).values("last_read_reply_id")[:1]
    rows = (
        Message.objects.filter(
            thread_root_id__in=root_ids,
            room_id=F("thread_root__room_id"),
            hidden=False,
            kind=Message.Kind.USER,
        )
        .annotate(
            read_cursor=Coalesce(
                Subquery(cursor), Value(0), output_field=BigIntegerField()
            )
        )
        .values("thread_root_id", "read_cursor")
        .annotate(
            reply_count=Count("id"),
            latest_reply_id=Max("id"),
            last_reply_at=Max("time"),
            unread_count=Count(
                "id", filter=Q(id__gt=F("read_cursor")) & ~Q(author_id=profile.id)
            ),
        )
        .order_by()
    )
    result = {
        root_id: {
            "reply_count": 0,
            "unread_count": 0,
            "latest_reply_id": None,
            "last_reply_at": None,
            "following": bool(
                participations.get(root_id) and participations[root_id].following
            ),
        }
        for root_id in root_ids
    }
    for row in rows:
        root_id = row.pop("thread_root_id")
        row.pop("read_cursor")
        result[root_id].update(row)
    return result


def thread_render_context(messages, profile):
    return {"thread_summaries": thread_summaries(messages, profile)}
