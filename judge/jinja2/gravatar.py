import hashlib

from django.utils.http import urlencode

from judge.utils.unicode import utf8bytes
from judge.models import Profile
from . import registry


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
