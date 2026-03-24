"""
Cache operations for chatbot conversations.
Stores conversation history in Django cache with TTL.
"""

import time
from django.core.cache import cache

CACHE_KEY_PREFIX = "chatbot"
CACHE_TTL = 86400  # 1 day in seconds
MAX_STORED_MESSAGES = 50


def get_cache_key(user_id, problem_code):
    """Generate cache key for a user's conversation with a problem."""
    return f"{CACHE_KEY_PREFIX}:{user_id}:{problem_code}"


def get_conversation(user_id, problem_code):
    """
    Get conversation from cache or return empty structure.

    Args:
        user_id: The user's ID
        problem_code: The problem code

    Returns:
        Dict with messages list, timestamps, and model selection
    """
    key = get_cache_key(user_id, problem_code)
    conversation = cache.get(key)

    if conversation is None:
        # Import here to avoid circular imports
        from llm_service.config import get_config

        config = get_config()
        return {
            "messages": [],
            "model": config.get_chatbot_default_model(),
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }

    # Ensure model field exists for existing conversations
    if "model" not in conversation:
        from llm_service.config import get_config

        config = get_config()
        conversation["model"] = config.get_chatbot_default_model()

    return conversation


def save_conversation(user_id, problem_code, conversation):
    """
    Save conversation to cache with TTL.

    Args:
        user_id: The user's ID
        problem_code: The problem code
        conversation: Dict with messages list and timestamps
    """
    key = get_cache_key(user_id, problem_code)
    conversation["updated_at"] = int(time.time())
    msgs = conversation.get("messages")
    if msgs and len(msgs) > MAX_STORED_MESSAGES:
        conversation["messages"] = msgs[-MAX_STORED_MESSAGES:]
    cache.set(key, conversation, timeout=CACHE_TTL)


def clear_conversation(user_id, problem_code):
    """
    Delete conversation from cache.

    Args:
        user_id: The user's ID
        problem_code: The problem code
    """
    key = get_cache_key(user_id, problem_code)
    cache.delete(key)


def add_message(user_id, problem_code, role, content, tool_calls=None):
    """
    Add a message to the conversation history.

    Args:
        user_id: The user's ID
        problem_code: The problem code
        role: "user" or "assistant"
        content: Message content
        tool_calls: Optional list of tool call info (for assistant messages)

    Returns:
        Updated conversation dict
    """
    conversation = get_conversation(user_id, problem_code)

    message = {
        "role": role,
        "content": content,
        "timestamp": int(time.time()),
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    conversation["messages"].append(message)
    save_conversation(user_id, problem_code, conversation)

    return conversation


def get_model(user_id, problem_code):
    """
    Get the selected model for a conversation.

    Args:
        user_id: The user's ID
        problem_code: The problem code

    Returns:
        Model ID string
    """
    conversation = get_conversation(user_id, problem_code)
    return conversation.get("model")


def delete_message(user_id, problem_code, message_index):
    """
    Delete a message from conversation history.
    If deleting a user message, also deletes the following assistant response.

    Returns:
        True if deletion succeeded, False otherwise
    """
    conversation = get_conversation(user_id, problem_code)
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
    save_conversation(user_id, problem_code, conversation)
    return True


def set_model(user_id, problem_code, model_id):
    """
    Set the model for a conversation.

    Args:
        user_id: The user's ID
        problem_code: The problem code
        model_id: The model ID to set

    Returns:
        True if successful, False if model is invalid
    """
    # Validate model ID
    from llm_service.config import get_config

    config = get_config()
    valid_models = [m["id"] for m in config.get_chatbot_supported_models()]

    if model_id not in valid_models:
        return False

    conversation = get_conversation(user_id, problem_code)
    conversation["model"] = model_id
    save_conversation(user_id, problem_code, conversation)
    return True
