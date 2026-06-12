"""
Cache operations for chatbot conversations.
Stores conversation history in Django cache with TTL.
"""

import time
from django.core.cache import cache

from llm_service.config import get_config

CACHE_KEY_PREFIX = "chatbot"
CACHE_TTL = 86400  # 1 day in seconds
MAX_STORED_MESSAGES = 50


def get_cache_key(user_id, problem_id):
    """Generate cache key for a user's conversation with a problem."""
    return f"{CACHE_KEY_PREFIX}:{user_id}:problem_id:{problem_id}"


def get_legacy_cache_key(user_id, problem_code):
    """Generate the pre-problem-id cache key for migration/backward compatibility."""
    return f"{CACHE_KEY_PREFIX}:{user_id}:{problem_code}"


def get_conversation(user_id, problem_id, legacy_problem_code=None):
    """
    Get conversation from cache or return empty structure.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID
        legacy_problem_code: Optional old problem-code key to migrate

    Returns:
        Dict with messages list, timestamps, and model selection
    """
    key = get_cache_key(user_id, problem_id)
    conversation = cache.get(key)
    if conversation is None and legacy_problem_code:
        legacy_key = get_legacy_cache_key(user_id, legacy_problem_code)
        conversation = cache.get(legacy_key)
        if conversation is not None:
            cache.set(key, conversation, timeout=CACHE_TTL)
            cache.delete(legacy_key)

    config = get_config()

    if conversation is None:
        return {
            "messages": [],
            "model": config.get_chatbot_default_model(),
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }

    # Stale IDs (e.g. an old GPT version that was removed from the registry)
    # would otherwise be sent to Poe and fail with an unknown-bot error.
    valid_models = {m["id"] for m in config.get_chatbot_supported_models()}
    if conversation.get("model") not in valid_models:
        conversation["model"] = config.get_chatbot_default_model()

    return conversation


def save_conversation(user_id, problem_id, conversation):
    """
    Save conversation to cache with TTL.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID
        conversation: Dict with messages list and timestamps
    """
    key = get_cache_key(user_id, problem_id)
    conversation["updated_at"] = int(time.time())
    msgs = conversation.get("messages")
    if msgs and len(msgs) > MAX_STORED_MESSAGES:
        conversation["messages"] = msgs[-MAX_STORED_MESSAGES:]
    cache.set(key, conversation, timeout=CACHE_TTL)


def clear_conversation(user_id, problem_id, legacy_problem_code=None):
    """
    Delete conversation from cache.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID
        legacy_problem_code: Optional old problem-code key to delete
    """
    cache.delete(get_cache_key(user_id, problem_id))
    if legacy_problem_code:
        cache.delete(get_legacy_cache_key(user_id, legacy_problem_code))


def add_message(user_id, problem_id, role, content, tool_calls=None):
    """
    Add a message to the conversation history.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID
        role: "user" or "assistant"
        content: Message content
        tool_calls: Optional list of tool call info (for assistant messages)

    Returns:
        Updated conversation dict
    """
    conversation = get_conversation(user_id, problem_id)

    message = {
        "role": role,
        "content": content,
        "timestamp": int(time.time()),
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    conversation["messages"].append(message)
    save_conversation(user_id, problem_id, conversation)

    return conversation


def get_model(user_id, problem_id):
    """
    Get the selected model for a conversation.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID

    Returns:
        Model ID string
    """
    conversation = get_conversation(user_id, problem_id)
    return conversation.get("model")


def delete_message(user_id, problem_id, message_index, legacy_problem_code=None):
    """
    Delete a message from conversation history.
    If deleting a user message, also deletes the following assistant response.

    Returns:
        True if deletion succeeded, False otherwise
    """
    conversation = get_conversation(
        user_id,
        problem_id,
        legacy_problem_code=legacy_problem_code,
    )
    messages = conversation.get("messages", [])

    if message_index < 0 or message_index >= len(messages):
        return False

    target = messages[message_index]

    if target["role"] == "user":
        # Delete user message and the following assistant response if exists
        next_idx = message_index + 1
        if next_idx < len(messages) and messages[next_idx]["role"] == "assistant":
            del messages[next_idx]
        del messages[message_index]
    else:
        del messages[message_index]

    conversation["messages"] = messages
    save_conversation(user_id, problem_id, conversation)
    return True


def set_model(user_id, problem_id, model_id, legacy_problem_code=None):
    """
    Set the model for a conversation.

    Args:
        user_id: The user's ID
        problem_id: The problem's persistent database ID
        model_id: The model ID to set

    Returns:
        True if successful, False if model is invalid
    """
    config = get_config()
    valid_models = [m["id"] for m in config.get_chatbot_supported_models()]

    if model_id not in valid_models:
        return False

    conversation = get_conversation(
        user_id,
        problem_id,
        legacy_problem_code=legacy_problem_code,
    )
    conversation["model"] = model_id
    save_conversation(user_id, problem_id, conversation)
    return True
