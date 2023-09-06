from cryptography.fernet import Fernet
import hmac
import hashlib

from django.conf import settings
from django.db.models import OuterRef, Count, Subquery, IntegerField, Q
from django.db.models.functions import Coalesce

from chat_box.models import Ignore, Message, UserRoom, Room

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
    except Exception as e:
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


def get_unread_boxes(profile):
    ignored_rooms = Ignore.get_ignored_rooms(profile)
    unread_boxes = (
        UserRoom.objects.filter(user=profile, unread_count__gt=0)
        .exclude(room__in=ignored_rooms)
        .count()
    )

    return unread_boxes
