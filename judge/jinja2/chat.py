from . import registry
from chat_box.utils import encrypt_url


@registry.function
def chat_param(request_profile, profile):
    return encrypt_url(request_profile.id, profile.id)
