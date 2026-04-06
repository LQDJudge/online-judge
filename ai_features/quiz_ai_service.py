"""
Quiz AI service for LQDOJ
Provides AI-powered markdown improvement and explanation generation for quiz questions.
Uses LLMService (Poe API) with quiz-specific prompts — distinct from the competitive
programming MarkdownImprover which targets problem statements with ####Input/Output format.
"""

import json
import logging
from typing import Dict, Any

from llm_service.llm_api import LLMService
from llm_service.config import get_config
from llm_service.prompt_guidelines import get_markdown_rules_for_prompt

logger = logging.getLogger(__name__)

QUESTION_MARKDOWN_SYSTEM_PROMPT = """You are an expert at formatting educational quiz questions in markdown.
Your task is to improve the markdown formatting of a quiz question's content.

IMPORTANT FORMATTING RULES:
1. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
2. Use \\cdot for multiplication in math mode
3. Format large numbers with scientific notation where appropriate (e.g., $10^9$)
4. Use clean markdown structure: headers, bullet points, bold/italic for emphasis
5. Preserve the original language (Vietnamese or English)
6. For code snippets, use ``` code blocks with appropriate language tags
7. For tables, use proper markdown table syntax
8. Do NOT add competitive programming formatting (no ####Input, ####Output, no !!! admonitions)
9. This is a quiz question — focus on clarity and readability for students

{markdown_rules}

CRITICAL INSTRUCTIONS:
- Maintain the EXACT same content/meaning, just improve formatting
- Do NOT add or remove any information
- Output ONLY the improved markdown, no explanations or commentary"""


def _build_explanation_system_prompt():
    return """You are an expert educator creating explanations for quiz questions.
Your task is to either generate a new explanation or improve an existing one.

FORMATTING RULES:
1. Use LaTeX math notation with $ for inline math where appropriate
2. Use clean markdown: bullet points, bold, headers for organization
3. Preserve the original language (Vietnamese or English)
4. Be thorough but concise — students should understand WHY the answer is correct

EXPLANATION GUIDELINES BY QUESTION TYPE:
- Multiple Choice (MC): Explain why the correct answer is right AND briefly why each wrong option is incorrect
- Multiple Answer (MA): Explain why each correct answer is included and why wrong options are excluded
- True/False (TF): Explain the reasoning behind why the statement is true or false
- Short Answer (SA): Explain why the accepted answers are correct, mention any edge cases
- Essay (ES): Provide key points that a good answer should cover, with a model answer outline

CRITICAL INSTRUCTIONS:
- If generating a new explanation: create a comprehensive explanation from the question context
- If improving an existing explanation: enhance formatting, clarity, add missing reasoning
- Output ONLY the explanation content in markdown, no meta-commentary"""


class QuizAIService:
    """Service for AI-powered quiz question features using LLM"""

    def __init__(self):
        self.config = get_config()
        self.llm_service = LLMService(
            api_key=self.config.api_key,
            bot_name=self.config.get_bot_name_for_markdown(),
            sleep_time=self.config.sleep_time,
        )

    def improve_question_markdown(
        self, content: str, choices_json: str = ""
    ) -> Dict[str, Any]:
        """
        Improve the markdown formatting of a quiz question's content field and optionally its answer choices.

        Args:
            content: The raw question content text
            choices_json: Optional JSON string of answer choices to also improve

        Returns:
            Dict with 'success', 'improved_markdown', optional 'improved_choices', and optional 'error'
        """
        if not content or not content.strip():
            return {
                "success": False,
                "error": "No content provided",
            }

        markdown_rules = get_markdown_rules_for_prompt(start_number=10)
        system_prompt = QUESTION_MARKDOWN_SYSTEM_PROMPT.format(
            markdown_rules=markdown_rules
        )

        # Build choices section if provided
        choices_section = ""
        choices_list = []
        choice_ids = []
        if choices_json:
            try:
                parsed_choices = json.loads(choices_json)
                if isinstance(parsed_choices, list) and parsed_choices:
                    # Extract text and IDs from {id, text} objects
                    for c in parsed_choices:
                        if isinstance(c, dict):
                            choices_list.append(c.get("text", ""))
                            choice_ids.append(c.get("id", chr(65 + len(choice_ids))))
                        else:
                            choices_list.append(str(c))
                            choice_ids.append(chr(65 + len(choice_ids)))
                    if choices_list:
                        choices_section = (
                            "\n\nANSWER CHOICES (also improve formatting for each):\n"
                        )
                        for cid, text in zip(choice_ids, choices_list):
                            choices_section += f"  {cid}. {text}\n"
                        choices_section += '\nFor the choices, output them in the SAME order as a JSON array of strings, e.g. ["improved choice 1", "improved choice 2", ...]. Place the improved choices JSON on a separate line after the marker IMPROVED_CHOICES_JSON:'
            except (json.JSONDecodeError, TypeError):
                choices_list = []

        user_prompt = f"""Please improve the markdown formatting of the following quiz question content.
Keep the same meaning and language, just improve the formatting.

ORIGINAL CONTENT:
{content}{choices_section}

OUTPUT: Provide the reformatted question content markdown first.{' Then on a new line write IMPROVED_CHOICES_JSON: followed by the improved choices as a JSON array of strings.' if choices_list else ''} No other commentary."""

        try:
            response = self.llm_service.call_llm(user_prompt, system_prompt)

            if response:
                improved = response.strip()

                # Remove ```markdown or ``` wrappers if present
                if improved.startswith("```markdown"):
                    improved = improved[11:]
                elif improved.startswith("```"):
                    improved = improved[3:]

                if improved.endswith("```"):
                    improved = improved[:-3]

                improved = improved.strip()

                # Extract improved choices if present
                improved_choices = None
                if choices_list and "IMPROVED_CHOICES_JSON:" in improved:
                    parts = improved.split("IMPROVED_CHOICES_JSON:", 1)
                    improved = parts[0].strip()
                    choices_raw = parts[1].strip()
                    # Remove ``` wrappers from choices JSON
                    if choices_raw.startswith("```json"):
                        choices_raw = choices_raw[7:]
                    elif choices_raw.startswith("```"):
                        choices_raw = choices_raw[3:]
                    if choices_raw.endswith("```"):
                        choices_raw = choices_raw[:-3]
                    choices_raw = choices_raw.strip()
                    try:
                        parsed = json.loads(choices_raw)
                        if isinstance(parsed, list):
                            # Convert string array back to {id, text} objects
                            improved_choices = []
                            for i, text in enumerate(parsed):
                                cid = (
                                    choice_ids[i]
                                    if i < len(choice_ids)
                                    else chr(65 + i)
                                )
                                if isinstance(text, dict):
                                    # LLM returned objects already
                                    improved_choices.append(text)
                                else:
                                    improved_choices.append(
                                        {"id": cid, "text": str(text)}
                                    )
                    except (json.JSONDecodeError, TypeError):
                        pass

                if improved:
                    result = {
                        "success": True,
                        "improved_markdown": improved,
                    }
                    if improved_choices is not None:
                        result["improved_choices"] = improved_choices
                    return result

            return {
                "success": False,
                "error": "Failed to get response from LLM",
            }

        except Exception as e:
            logger.error(f"Error improving question markdown: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def generate_or_improve_explanation(
        self,
        question_content: str,
        question_type: str,
        choices_json: str = "",
        correct_answers_json: str = "",
        existing_explanation: str = "",
        rough_ideas: str = "",
    ) -> Dict[str, Any]:
        """
        Generate a new explanation or improve an existing one for a quiz question.

        Args:
            question_content: The question text
            question_type: Question type code (MC, MA, TF, SA, ES)
            choices_json: JSON string of choices list
            correct_answers_json: JSON string of correct answers
            existing_explanation: Existing explanation text (empty = generate new)

        Returns:
            Dict with 'success', 'explanation_content', 'mode', and optional 'error'
        """
        if not question_content or not question_content.strip():
            return {
                "success": False,
                "error": "No question content provided",
            }

        type_names = {
            "MC": "Multiple Choice",
            "MA": "Multiple Answer",
            "TF": "True/False",
            "SA": "Short Answer",
            "ES": "Essay",
        }
        type_name = type_names.get(question_type, question_type)

        # Parse choices and answers for context
        choices_text = ""
        if choices_json:
            try:
                choices = json.loads(choices_json)
                if isinstance(choices, list):
                    for i, choice in enumerate(choices):
                        label = chr(65 + i)  # A, B, C, D...
                        text = (
                            choice.get("text", str(choice))
                            if isinstance(choice, dict)
                            else str(choice)
                        )
                        choices_text += f"  {label}. {text}\n"
            except (json.JSONDecodeError, TypeError):
                choices_text = f"  (raw: {choices_json})\n"

        answers_text = ""
        if correct_answers_json:
            try:
                answers = json.loads(correct_answers_json)
                if isinstance(answers, dict):
                    ans = answers.get("answers", answers)
                    answers_text = f"  {ans}"
                else:
                    answers_text = f"  {answers}"
            except (json.JSONDecodeError, TypeError):
                answers_text = f"  {correct_answers_json}"

        mode = (
            "improve"
            if existing_explanation and existing_explanation.strip()
            else "generate"
        )
        system_prompt = _build_explanation_system_prompt()

        # Build rough ideas section (following solution_generator.py pattern)
        rough_ideas_section = ""
        if rough_ideas and rough_ideas.strip():
            rough_ideas_section = f"""

USER'S ROUGH IDEAS/DRAFT:
The user has provided the following rough ideas or draft. Please improve and expand upon these ideas, maintaining their core approach while making the explanation clearer, more structured, and more educational:

{rough_ideas}

"""

        if mode == "generate":
            user_prompt = f"""Generate a comprehensive explanation for the following quiz question.

Question Type: {type_name}
Question Content:
{question_content}
"""
            if choices_text:
                user_prompt += f"\nChoices:\n{choices_text}"
            if answers_text:
                user_prompt += f"\nCorrect Answer(s): {answers_text}"

            user_prompt += rough_ideas_section
            user_prompt += (
                "\nOUTPUT: Provide ONLY the explanation in markdown, nothing else."
            )
        else:
            user_prompt = f"""Improve the following explanation for a quiz question.
Enhance formatting, clarity, and add any missing reasoning.

Question Type: {type_name}
Question Content:
{question_content}
"""
            if choices_text:
                user_prompt += f"\nChoices:\n{choices_text}"
            if answers_text:
                user_prompt += f"\nCorrect Answer(s): {answers_text}"

            user_prompt += rough_ideas_section
            user_prompt += f"""
EXISTING EXPLANATION:
{existing_explanation}

OUTPUT: Provide ONLY the improved explanation in markdown, nothing else."""

        try:
            response = self.llm_service.call_llm(user_prompt, system_prompt)

            if response:
                result = response.strip()

                # Remove ```markdown or ``` wrappers if present
                if result.startswith("```markdown"):
                    result = result[11:]
                elif result.startswith("```"):
                    result = result[3:]

                if result.endswith("```"):
                    result = result[:-3]

                result = result.strip()

                if result:
                    return {
                        "success": True,
                        "explanation_content": result,
                        "mode": mode,
                    }

            return {
                "success": False,
                "error": "Failed to get response from LLM",
            }

        except Exception as e:
            logger.error(f"Error generating/improving explanation: {e}")
            return {
                "success": False,
                "error": str(e),
            }


# Global instance
_quiz_ai_service = None


def get_quiz_ai_service() -> QuizAIService:
    """Get global Quiz AI Service instance"""
    global _quiz_ai_service
    if _quiz_ai_service is None:
        _quiz_ai_service = QuizAIService()
    return _quiz_ai_service
