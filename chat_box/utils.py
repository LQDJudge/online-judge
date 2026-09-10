import base64
import hashlib
import hmac
import json
import secrets
import time

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Value
from django.db.models.functions import Coalesce

from cryptography.fernet import Fernet

from chat_box.models import (
    CHAT_REACTION_CODES,
    Ignore,
    Message,
    MessageReaction,
    UserRoom,
)


def _derive_secret_bytes(purpose):
    return hashlib.sha256((str(settings.SECRET_KEY) + ":" + purpose).encode()).digest()


def _derive_fernet_key(purpose):
    return base64.urlsafe_b64encode(_derive_secret_bytes(purpose))


fernet = Fernet(_derive_fernet_key("chat.url.v1"))


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
            _derive_secret_bytes("chat.channel.v1"),
            channel.encode(),
            hashlib.sha512,
        ).hexdigest()[:16]
        + "%s" % channel
    )


def create_chat_event_grant(profile_id, room_ids, channels, lifetime_seconds=900):
    if isinstance(room_ids, int):
        room_ids = [room_ids]
    room_ids = sorted(set(room_ids))
    payload = {
        "channels": sorted(channels),
        "exp": int(time.time()) + lifetime_seconds,
        "nonce": secrets.token_urlsafe(12),
        "room_ids": room_ids,
        "user_id": profile_id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    key = str(settings.EVENT_DAEMON_KEY or settings.SECRET_KEY).encode()
    signature = hmac.new(key, encoded, hashlib.sha256).hexdigest().encode()
    return (encoded + b"." + signature).decode()


def get_unread_boxes(profile):
    ignored_rooms = Ignore.get_ignored_room_ids(profile)
    unread_message = Message.objects.filter(
        room_id=OuterRef("room_id"),
        id__gt=Coalesce(OuterRef("last_read_message_id"), Value(0)),
        kind="user",
        hidden=False,
    ).exclude(author_id=profile.id)
    return (
        UserRoom.objects.filter(
            user=profile,
            state=UserRoom.State.ACTIVE,
            is_hidden=False,
            room__archived_at__isnull=True,
        )
        .exclude(room__singleton_key="lobby")
        .exclude(room_id__in=ignored_rooms)
        .annotate(has_unread=Exists(unread_message))
        .filter(has_unread=True)
        .count()
    )


def get_reactions_summary(message_ids, user, include_my_reaction=True):
    """Batched reaction summary for a set of messages.

    Returns {message_id: {"counts": {code: n}, "total": N, "my_reaction": code|None}}.
    Uses a constant number of queries (one grouped count + one for the viewer's own
    reactions) regardless of how many messages are passed -- avoids N+1 on the
    message list.

    ``include_my_reaction=False`` skips the second query and leaves my_reaction as
    None -- for callers (e.g. react_message) that already know the viewer's
    resulting reaction and will fill it in themselves.
    """
    message_ids = list(message_ids)
    result = {
        mid: {"counts": {}, "total": 0, "my_reaction": None} for mid in message_ids
    }
    if not message_ids:
        return result

    rows = (
        MessageReaction.objects.filter(
            message_id__in=message_ids, reaction__in=CHAT_REACTION_CODES
        )
        .values("message_id", "reaction")
        .annotate(c=Count("id"))
    )
    for row in rows:
        entry = result[row["message_id"]]
        entry["counts"][row["reaction"]] = row["c"]
        entry["total"] += row["c"]

    if include_my_reaction and user is not None and getattr(user, "id", None):
        mine = MessageReaction.objects.filter(
            message_id__in=message_ids, user=user, reaction__in=CHAT_REACTION_CODES
        ).values_list("message_id", "reaction")
        for mid, reaction in mine:
            result[mid]["my_reaction"] = reaction

    return result
