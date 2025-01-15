import hashlib

from django.contrib.auth.models import AbstractUser
from django.utils.http import urlencode

from judge.utils.unicode import utf8bytes
from judge.models import Profile
from . import registry


@registry.function
def gravatar(profile_id, size=80, default=None, profile_image=None, email=None):
    profile = Profile(id=profile_id) if profile_id else None
    if profile and not profile.is_muted:
        if profile_image:
            return profile_image
        if profile and profile.profile_image_url:
            return profile.profile_image_url
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
