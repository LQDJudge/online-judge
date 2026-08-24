from . import registry
from judge.utils.voting import make_vote_token


@registry.function
def vote_token(profile, kind, object_id):
    return make_vote_token(profile, kind, object_id)
