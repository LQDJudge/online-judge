import time
import uuid

from django.core.cache import cache

CACHE_TTL = 86400
MAX_MESSAGES = 30


def _key(user_id, post_id):
    return f"blog_composer:{user_id}:{post_id or 'new'}"


def get_session(user_id, post_id=None):
    session = cache.get(_key(user_id, post_id))
    if session is None:
        return {
            "messages": [],
            "proposal": None,
            "settings": {},
            "updated_at": int(time.time()),
        }
    session.setdefault("settings", {})
    return session


def save_session(user_id, post_id, session):
    messages = session.get("messages", [])
    session["messages"] = messages[-MAX_MESSAGES:]
    session["updated_at"] = int(time.time())
    cache.set(_key(user_id, post_id), session, CACHE_TTL)


def clear_session(user_id, post_id=None):
    cache.delete(_key(user_id, post_id))


def save_proposal(user_id, post_id, proposal, session=None):
    if session is None:
        session = get_session(user_id, post_id)
    proposal["id"] = uuid.uuid4().hex
    session["proposal"] = proposal
    save_session(user_id, post_id, session)
    return proposal


def get_proposal(user_id, post_id, proposal_id):
    proposal = get_session(user_id, post_id).get("proposal")
    if proposal and proposal.get("id") == proposal_id:
        return proposal
    return None
