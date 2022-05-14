from cryptography.fernet import Fernet

from django.conf import settings

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
