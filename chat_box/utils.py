from cryptography.fernet import Fernet
import hmac
import hashlib

from django.conf import settings
from django.db.models import Count

from chat_box.models import Ignore, MessageReaction, UserRoom

from judge.caching import cache_wrapper

secret_key = settings.CHAT_SECRET_KEY
fernet = Fernet(secret_key)


def encrypt_url(creator_id, other_id):
    message = str(creator_id) + "_" + str(other_id)
    return fernet.encrypt(message.encode()).decode()


def decrypt_url(message_encrypted):
    try:
        dec_message = fernet.decrypt(message_encrypted.encode()).decode()
        creator_id, other_id = dec_message.split("_")
        return int(creator_id), int(other_id)
    except Exception:
        return None, None


def encrypt_channel(channel):
    return (
        hmac.new(
            settings.CHAT_SECRET_KEY.encode(),
            channel.encode(),
            hashlib.sha512,
        ).hexdigest()[:16]
        + "%s" % channel
    )


@cache_wrapper(prefix="gub2")
def get_unread_boxes(profile):
    ignored_rooms = Ignore.get_ignored_room_ids(profile)
    unread_boxes = (
        UserRoom.objects.filter(user=profile, unread_count__gt=0)
        .exclude(room__in=ignored_rooms)
        .count()
    )

    return unread_boxes


def get_reactions_summary(message_ids, user):
    """Batched reaction summary for a set of messages.

    Returns {message_id: {"counts": {code: n}, "total": N, "my_reaction": code|None}}.
    Uses a constant number of queries (one grouped count + one for the viewer's own
    reactions) regardless of how many messages are passed -- avoids N+1 on the
    message list.
    """
    message_ids = list(message_ids)
    result = {
        mid: {"counts": {}, "total": 0, "my_reaction": None} for mid in message_ids
    }
    if not message_ids:
        return result

    rows = (
        MessageReaction.objects.filter(message_id__in=message_ids)
        .values("message_id", "reaction")
        .annotate(c=Count("id"))
    )
    for row in rows:
        entry = result[row["message_id"]]
        entry["counts"][row["reaction"]] = row["c"]
        entry["total"] += row["c"]

    if user is not None and getattr(user, "id", None):
        mine = MessageReaction.objects.filter(
            message_id__in=message_ids, user=user
        ).values_list("message_id", "reaction")
        for mid, reaction in mine:
            result[mid]["my_reaction"] = reaction

    return result
