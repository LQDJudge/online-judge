from cryptography.fernet import Fernet

from django.conf import settings
from django.db.models import OuterRef, Count, Subquery, IntegerField
from django.db.models.functions import Coalesce

from chat_box.models import Ignore, Message, UserRoom

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


def get_unread_boxes(profile):
    ignored_users = Ignore.get_ignored_users(profile)

    mess = (
        Message.objects.filter(room=OuterRef("room"), time__gte=OuterRef("last_seen"))
        .exclude(author=profile)
        .exclude(author__in=ignored_users)
        .order_by()
        .values("room")
        .annotate(unread_count=Count("pk"))
        .values("unread_count")
    )

    unread_boxes = (
        UserRoom.objects.filter(user=profile, room__isnull=False)
        .annotate(
            unread_count=Coalesce(Subquery(mess, output_field=IntegerField()), 0),
        )
        .filter(unread_count__gte=1)
        .count()
    )

    return unread_boxes
