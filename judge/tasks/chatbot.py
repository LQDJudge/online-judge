"""
Celery task for chatbot LLM processing.
Handles conversation with tool use for problem author assistance.
"""

import json
import logging
import re
import time

from celery import shared_task

from judge.chatbot.cache import get_conversation, save_conversation
from judge.chatbot.tools import CHATBOT_TOOLS, execute_tool
from judge.markdown import markdown as render_markdown

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an AI assistant helping problem authors create and manage competitive programming problems on LQDOJ (Le Quy Don Online Judge).

You have access to the following tools to help you answer questions:

{tool_descriptions}

TOOL USAGE:
- When you need information about the problem, call the appropriate tool first
- To call a tool, use this exact format:
<tool_call>{{"tool": "tool_name"}}</tool_call>

- After receiving tool results, provide your helpful response

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
- RANDOM CASES: various sizes within each subtask constraint"""


def _build_tool_descriptions():
    """Build formatted tool descriptions for system prompt."""
    descriptions = []
    for name, tool in CHATBOT_TOOLS.items():
        descriptions.append(f"- {name}: {tool['description']}")
    return "\n".join(descriptions)


def _build_conversation_context(messages, max_messages=10):
    """Build conversation history text for context."""
    if not messages:
        return ""

    # Take last N messages
    recent = messages[-max_messages:]
    context_parts = []

    for msg in recent:
        role = "User" if msg["role"] == "user" else "Assistant"
        content = msg["content"]
        # Truncate long messages in history
        if len(content) > 1000:
            content = content[:1000] + "..."
        context_parts.append(f"{role}: {content}")

    return "\n\n".join(context_parts)


def _extract_tool_calls(response):
    """Extract tool calls from LLM response."""
    pattern = r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
    matches = re.findall(pattern, response, re.DOTALL)

    tool_calls = []
    for match in matches:
        try:
            tool_data = json.loads(match)
            tool_name = tool_data.get("tool")
            if tool_name and tool_name in CHATBOT_TOOLS:
                tool_calls.append(tool_name)
        except json.JSONDecodeError:
            continue

    return tool_calls


def _clean_response(response):
    """Remove tool call markers from response."""
    pattern = r"<tool_call>\s*\{.*?\}\s*</tool_call>"
    cleaned = re.sub(pattern, "", response, flags=re.DOTALL)
    # Clean up extra whitespace
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _process_with_tools(
    llm, problem, user_message, conversation_history, system_prompt
):
    """Process message with potential tool use."""
    # Build initial prompt
    history_text = _build_conversation_context(conversation_history)

    user_prompt = f"""CONVERSATION HISTORY:
{history_text}

CURRENT USER MESSAGE:
{user_message}

Respond helpfully. If you need information from tools, use the tool_call format."""

    # First LLM call
    response = llm.call_llm(user_prompt, system_prompt)

    if not response:
        return {
            "content": "Xin lỗi, tôi gặp lỗi khi xử lý. Vui lòng thử lại.",
            "tool_calls": [],
        }

    # Check for tool calls
    tool_names = _extract_tool_calls(response)

    if not tool_names:
        # No tools needed, return cleaned response
        return {"content": _clean_response(response), "tool_calls": []}

    # Execute tools and collect results
    tool_results = []
    tool_call_info = []

    for tool_name in tool_names:
        result = execute_tool(tool_name, problem)
        tool_results.append(f"[{tool_name}]:\n{result}")
        tool_call_info.append(
            {
                "tool": tool_name,
                "result_summary": result[:200] + "..." if len(result) > 200 else result,
            }
        )

    # Make follow-up call with tool results
    # Add generator script reminder if generator template was requested
    generator_reminder = ""
    if "get_generator_template" in tool_names:
        generator_reminder = """

COMPREHENSIVE TEST GENERATION REMINDERS:
Generate tests like a competitive programming problemsetter! Include:
1. EDGE CASES: min values (n=1), max values (n=10^9), boundary cases - use EXACT values
2. PROBLEM-SPECIFIC: For trees (line, star, binary), arrays (sorted, reverse), etc.
3. RANDOM CASES: Various sizes per subtask

Generator should support multiple MODES (e.g., "min", "max", "random", "line", "star").
Match test count to subtask percentages.

GENERATOR SCRIPT FORMAT:
- Comment LINES (starting with # or //) are allowed for labeling sections
- NEVER use inline comments (e.g., "random 100 1001 // test" will FAIL)
- Each non-comment line = space-separated args passed directly to generator"""

    followup_prompt = f"""CONVERSATION HISTORY:
{history_text}

CURRENT USER MESSAGE:
{user_message}

TOOL RESULTS:
{chr(10).join(tool_results)}{generator_reminder}

Based on the tool results above, provide a complete and helpful response to the user's question. Do not use tool_call format again."""

    final_response = llm.call_llm(followup_prompt, system_prompt)

    if final_response:
        final_response = _clean_response(final_response)
    else:
        final_response = "Xin lỗi, tôi gặp lỗi khi xử lý kết quả. Vui lòng thử lại."

    return {"content": final_response, "tool_calls": tool_call_info}


@shared_task(bind=True)
def chatbot_respond_task(self, user_id, problem_code, user_message):
    """
    Celery task to process chatbot message with tool use.

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

        # Add user message to history (markdown rendering done in view for immediate display)
        conversation["messages"].append(
            {
                "role": "user",
                "content": user_message,
                "timestamp": int(time.time()),
            }
        )

        # Initialize LLM service with selected model
        config = get_config()
        # Use model from conversation cache, fallback to config default
        selected_model = conversation.get("model") or config.get_bot_name_for_chatbot()
        llm = LLMService(
            api_key=config.api_key,
            bot_name=selected_model,
            sleep_time=config.sleep_time,
        )

        # Build system prompt with tool descriptions
        system_prompt = SYSTEM_PROMPT.format(
            tool_descriptions=_build_tool_descriptions()
        )

        # Process message (potentially with tools)
        result = _process_with_tools(
            llm=llm,
            problem=problem,
            user_message=user_message,
            conversation_history=conversation["messages"][:-1],  # Exclude current
            system_prompt=system_prompt,
        )

        # Render markdown to HTML for display
        try:
            content_html = render_markdown(result["content"])
        except Exception as md_error:
            logger.warning(f"Markdown rendering failed: {md_error}")
            # Fallback: escape HTML and convert newlines to <br>
            from django.utils.html import escape

            content_html = (
                '<div class="md-typeset content-description">'
                + escape(result["content"]).replace("\n", "<br>")
                + "</div>"
            )

        # Add assistant response to history (store raw content for context)
        conversation["messages"].append(
            {
                "role": "assistant",
                "content": result["content"],  # Raw markdown for conversation context
                "content_html": content_html,  # Rendered HTML for display
                "timestamp": int(time.time()),
                "tool_calls": result.get("tool_calls", []),
            }
        )

        # Save updated conversation
        save_conversation(user_id, problem_code, conversation)

        return {
            "success": True,
            "content": content_html,  # Return rendered assistant HTML
            "tool_calls": result.get("tool_calls", []),
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
