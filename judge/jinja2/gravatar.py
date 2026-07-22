import hashlib

from django.contrib.auth.models import AbstractUser
from django.utils.http import urlencode

from judge.utils.unicode import utf8bytes
from judge.models import Profile
from . import registry


def default_gravatar(size=80):
    args = {"d": "mp", "s": str(size), "f": "y"}
    return "//www.gravatar.com/avatar/00000000000000000000000000000000?" + urlencode(
        args
    )


def get_profile_for_gravatar(user):
    if isinstance(user, Profile):
        return user
    if isinstance(user, AbstractUser):
        return user.profile
    if isinstance(user, int):
        return Profile(id=user)
    raise ValueError("Expected profile, user, or profile id, got %s" % (type(user),))


@registry.function
def gravatar(profile_id, size=80):
    profile = Profile(id=profile_id)
    is_muted = profile.get_mute()

    if not is_muted:
        profile_image_url = profile.get_profile_image_url()
        if profile_image_url:
            return profile_image_url

    email = profile.get_email()
    default = is_muted

    gravatar_url = (
        "//www.gravatar.com/avatar/"
        + hashlib.md5(utf8bytes(email.strip().lower())).hexdigest()
        + "?"
    )
    args = {"d": "identicon", "s": str(size)}
    if default:
        args["f"] = "y"
    gravatar_url += urlencode(args)
    return gravatar_url


@registry.function
def public_gravatar(user, viewer=None, size=80):
    profile = get_profile_for_gravatar(user)
    if profile.should_hide_public_identity(viewer):
        return default_gravatar(size)
    return gravatar(profile.id, size)
