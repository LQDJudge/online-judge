from judge.utils.rewards import get_equipped_frame

from . import registry
from .gravatar import get_profile_for_gravatar


@registry.function
def public_avatar_frame(user, viewer=None):
    profile = get_profile_for_gravatar(user)
    if profile.should_hide_public_identity(viewer):
        return None
    return get_equipped_frame(profile.id)
