import base64
import hashlib

from django.conf import settings

from cryptography.fernet import Fernet

from judge.models.problem_review import ProblemReviewRun


def _get_vote_secret_key():
    digest = hashlib.sha256(
        (str(settings.SECRET_KEY) + ":judge.vote-token.v1").encode()
    ).digest()
    return base64.urlsafe_b64encode(digest)


_vote_fernet = Fernet(_get_vote_secret_key())


def make_vote_token(profile, kind, object_id):
    message = "%d_%s_%d" % (profile.id, kind, object_id)
    return _vote_fernet.encrypt(message.encode()).decode()


def decrypt_vote_token(profile, expected_kind, token):
    try:
        message = _vote_fernet.decrypt(token.encode()).decode()
        profile_id, kind, object_id = message.split("_", 2)
        if int(profile_id) != profile.id or kind != expected_kind:
            return None
        return int(object_id)
    except Exception:
        return None


def can_user_access_votable(user, obj):
    """Return whether a user may interact with votes for this object."""
    if obj is None:
        return False

    if isinstance(obj, ProblemReviewRun):
        return obj.problem.is_editable_by(user)

    access_check = getattr(obj, "is_accessible_by", None)
    if access_check is None:
        return False

    return access_check(user)
