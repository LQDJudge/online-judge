import hashlib

from django.contrib.auth.models import AbstractUser
from django.utils.http import urlencode

from judge.models import Profile
from judge.utils.unicode import utf8bytes
from . import registry


@registry.function
def gravatar(profile, size=80, default=None, profile_image=None, email=None):
    if profile_image:
        return profile_image
    if profile and profile.cached_profile_image:
        return profile.cached_profile_image.url
    if profile:
        email = email or profile.email
        if default is None:
            default = profile.is_muted
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
