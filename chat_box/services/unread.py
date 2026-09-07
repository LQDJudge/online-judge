from django.db import transaction
from django.utils import timezone

from chat_box.models import Message, UserRoom


@transaction.atomic
def mark_room_read(room, profile):
    membership = UserRoom.objects.select_for_update().get(
        room=room,
        user=profile,
        state=UserRoom.State.ACTIVE,
    )
    tail_id = (
        Message.objects.filter(room=room, kind=Message.Kind.USER, hidden=False)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    membership.last_read_message_id = tail_id
    membership.last_seen = timezone.now()
    membership.unread_count = 0
    membership.save(update_fields=["last_read_message_id", "last_seen", "unread_count"])
    return membership
