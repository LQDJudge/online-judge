"""Visibility changes for individual items awaiting moderation."""

from django.db import transaction
from django.db.models import Q

from chat_box.models import Message, Room, get_first_msg_id
from chat_box.services.events import broadcast_room_event
from judge.models import Comment


def set_moderated_visibility(obj, kind, hidden):
    if obj.hidden == hidden:
        return
    obj.hidden = hidden
    obj.save(update_fields=["hidden"])
    if kind == "comment":
        Comment.dirty_count_cache(obj.content_type_id, obj.object_id)
        Comment.dirty_cache(obj.pk)
        Comment.dirty_list_cache(obj.content_type_id, obj.object_id, obj.parent_id)
        transaction.on_commit(
            lambda: (
                Comment.dirty_count_cache(obj.content_type_id, obj.object_id),
                Comment.dirty_cache(obj.pk),
                Comment.dirty_list_cache(
                    obj.content_type_id, obj.object_id, obj.parent_id
                ),
            )
        )
        return
    if hidden:
        latest = (
            Message.objects.filter(
                room_id=obj.room_id, hidden=False, kind=Message.Kind.USER
            )
            .order_by("-id")
            .values("id", "time")
            .first()
        )
        Room.objects.filter(pk=obj.room_id, last_msg_id=obj.pk).update(
            last_msg_id=latest["id"] if latest else None,
            last_activity_at=latest["time"] if latest else None,
        )
    else:
        Room.objects.filter(pk=obj.room_id).filter(
            Q(last_msg_id__isnull=True) | Q(last_msg_id__lt=obj.pk)
        ).update(last_msg_id=obj.pk, last_activity_at=obj.time)
    get_first_msg_id.dirty(obj.room_id)
    transaction.on_commit(lambda: Room.dirty_cache(obj.room_id))
    transaction.on_commit(lambda: get_first_msg_id.dirty(obj.room_id))
    event_type = "message_hidden" if hidden else "message_restored"
    transaction.on_commit(
        lambda: broadcast_room_event(
            obj.room_id,
            {"type": event_type, "room": obj.room_id, "message": obj.pk},
        )
    )
