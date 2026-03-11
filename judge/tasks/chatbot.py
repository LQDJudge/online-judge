"""
Celery task for chatbot LLM processing.
Handles conversation with native Poe tool calling for problem author assistance.
"""

import logging
import time

from celery import shared_task

from judge.chatbot.cache import get_conversation, save_conversation
from judge.chatbot.tools import get_tool_definitions, get_tool_executables
from judge.markdown import markdown as render_markdown

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an AI assistant helping problem authors create and manage competitive programming problems on LQDOJ (Le Quy Don Online Judge).

You have access to tools to fetch problem information, templates, and reference code. Use them when needed.

GUIDELINES:
1. Use tools when you need specific information (problem details, code templates, etc.)
2. Respond in Vietnamese by default (unless the user writes in English)
3. Include code examples when relevant
4. Be concise but thorough
5. For checker/generator questions, always provide the relevant template first
6. If asked to write code, provide complete, working examples

GENERATOR SCRIPT BEST PRACTICES:
When suggesting Generator Scripts:
- USE comments (lines starting with # or //) to label subtasks for clarity
- Add EMPTY LINES between subtasks for readability
- Match the PERCENTAGE of tests per subtask to what's stated in the problem statement
- Each non-comment line = one test case = space-separated arguments for the generator
Example format:
# Subtask 1: n <= 100 (30% = 3 tests)
10 1001
50 1002
100 1003

# Subtask 2: n <= 10000 (70% = 7 tests)
1000 2001
5000 2002
...

COMMON TOOL USAGE:
- Problem info/metadata → get_problem_info
- Problem statement/description → get_problem_statement
- Writing checkers → get_checker_template, then get_existing_checker
- Writing generators → MUST call BOTH: get_problem_statement AND get_generator_template
- Reference solutions → get_ac_submissions
- Writing editorials → get_solution_template
- Test data help → get_test_data_docs

CRITICAL FOR GENERATORS:
When asked to write a test generator, you MUST:
1. Call BOTH tools: get_problem_statement AND get_generator_template
2. Follow the C++ template structure EXACTLY - do NOT create your own format or use Python
3. The generator takes command-line arguments: ./gen <mode> <args...> <seed>
4. Print INPUT to stdout, ANSWER to stderr

Generate COMPREHENSIVE test cases covering:
- EDGE CASES: min/max values, boundaries (use exact values, not random)
- PROBLEM-SPECIFIC: trees (line, star, binary), arrays (sorted, reverse), etc.
- RANDOM CASES: various sizes within each subtask constraint

GENERATOR SCRIPT FORMAT:
- Comment LINES (starting with # or //) are allowed for labeling sections
- NEVER use inline comments (e.g., "random 100 1001 // test" will FAIL)
- Each non-comment line = space-separated args passed directly to generator"""

MAX_HISTORY_MESSAGES = 10
# Reserve tokens for system prompt, current message, tool outputs, and response.
RESERVED_TOKENS = 50_000
# Rough approximation: 1 token ≈ 4 characters.
CHARS_PER_TOKEN = 4


def _max_history_chars(model_id):
    """Compute the character budget for history based on the model's context window."""
    from llm_service.config import get_config

    config = get_config()
    context_tokens = config.get_context_tokens(model_id)
    available_tokens = context_tokens - RESERVED_TOKENS
    return max(available_tokens, RESERVED_TOKENS) * CHARS_PER_TOKEN


def _get_recent_messages(messages, model_id, max_messages=MAX_HISTORY_MESSAGES):
    """Get recent conversation messages for LLM context.

    Applies both a message-count limit and a model-aware character budget,
    keeping the most recent messages that fit.
    """
    if not messages:
        return []

    max_chars = _max_history_chars(model_id)

    # Start from the most recent, collect until budget is spent
    candidates = messages[-max_messages:]
    result = []
    total_chars = 0

    for msg in reversed(candidates):
        content_len = len(msg.get("content", ""))
        if total_chars + content_len > max_chars:
            break
        total_chars += content_len
        result.append(msg)

    result.reverse()
    return result


@shared_task(bind=True)
def chatbot_respond_task(self, user_id, problem_code, user_message):
    """
    Celery task to process chatbot message with native Poe tool calling.

    Args:
        user_id: The user's ID
        problem_code: The problem code
        user_message: The user's message

    Returns:
        Dict with response data {success, content, tool_calls, error}
    """
    try:
        # Import here to avoid circular imports
        from judge.models import Problem
        from llm_service.config import get_config
        from llm_service.llm_api import LLMService

        # Get problem
        problem = Problem.objects.get(code=problem_code)

        # Load conversation history
        conversation = get_conversation(user_id, problem_code)

        # Add user message to history
        conversation["messages"].append(
            {
                "role": "user",
                "content": user_message,
                "timestamp": int(time.time()),
            }
        )

        # Initialize LLM service with selected model
        config = get_config()
        selected_model = conversation.get("model") or config.get_bot_name_for_chatbot()
        llm = LLMService(
            api_key=config.api_key,
            bot_name=selected_model,
            sleep_time=config.sleep_time,
        )

        # Build tool definitions and executables bound to this problem
        tool_definitions = get_tool_definitions()
        tool_executables = get_tool_executables(problem)

        # Get conversation history (excluding current message)
        recent_messages = _get_recent_messages(
            conversation["messages"][:-1], model_id=selected_model
        )

        # Call LLM with native tool calling
        response = llm.call_llm_with_history(
            conversation_messages=recent_messages,
            current_prompt=user_message,
            system_prompt=SYSTEM_PROMPT,
            tools=tool_definitions,
            tool_executables=tool_executables,
            strip_thinking=False,
        )

        if not response:
            response = "Xin lỗi, tôi gặp lỗi khi xử lý. Vui lòng thử lại."

        # Render markdown to HTML for display
        try:
            content_html = render_markdown(response)
        except Exception as md_error:
            logger.warning(f"Markdown rendering failed: {md_error}")
            from django.utils.html import escape

            content_html = (
                '<div class="md-typeset content-description">'
                + escape(response).replace("\n", "<br>")
                + "</div>"
            )

        # Add assistant response to history
        conversation["messages"].append(
            {
                "role": "assistant",
                "content": response,
                "content_html": content_html,
                "timestamp": int(time.time()),
            }
        )

        # Save updated conversation
        save_conversation(user_id, problem_code, conversation)

        return {
            "success": True,
            "content": content_html,
            "tool_calls": [],
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "error": f"Problem {problem_code} not found",
        }

    except Exception as e:
        logger.error(f"Chatbot error for {problem_code}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }
