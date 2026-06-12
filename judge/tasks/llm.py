"""
Celery tasks for LLM operations (solution generation, markdown improvement, etc.)
These tasks run asynchronously to avoid timeout issues with long-running LLM calls.
"""

import logging

from django.db.models import Max

from celery import shared_task

from ai_features.problem_tag_service import get_problem_tag_service
from ai_features.quiz_ai_service import get_quiz_ai_service
from ai_features.solution_code_generator import SolutionCodeGenerator
from judge.models import Language, Problem, ProblemType
from judge.models.problem_data import ProblemSolutionCode
from llm_service.config import get_config

logger = logging.getLogger(__name__)


def _resolve_problem_for_ai_task(problem_ref=None, problem_code=None):
    """
    Resolve a problem for AI tasks.

    New callers pass problem.id. Legacy queued tasks may still pass problem.code
    as the first positional argument, or as the problem_code keyword.
    """
    if problem_code:
        return Problem.objects.get(code=problem_code)
    if isinstance(problem_ref, int):
        return Problem.objects.get(id=problem_ref)
    return Problem.objects.get(code=problem_ref)


def _problem_ref_label(problem_ref=None, problem_code=None):
    return problem_code if problem_code else problem_ref


@shared_task(bind=True)
def generate_solution_task(
    self,
    problem_id=None,
    rough_ideas="",
    problem_code=None,
):
    """
    Celery task to generate solution for a problem using LLM.

    Args:
        problem_id: The problem's persistent database ID
        rough_ideas: Optional user-provided rough ideas
        problem_code: Legacy problem code for queued tasks created before the
            task argument was migrated to problem_id

    Returns:
        Dict with generation results (stored in Celery result backend)
    """
    try:
        problem = _resolve_problem_for_ai_task(problem_id, problem_code)
        tag_service = get_problem_tag_service()
        result = tag_service.generate_problem_solution(problem, rough_ideas=rough_ideas)

        return {
            "success": result["success"],
            "solution_content": result.get("solution_content"),
            "has_ac_code": result.get("has_ac_code", False),
            "ac_language": result.get("ac_language"),
            "error": result.get("error"),
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "error": (
                f"Problem {_problem_ref_label(problem_id, problem_code)} not found"
            ),
        }

    except Exception as e:
        logger.error(
            f"Error in generate_solution_task for "
            f"{_problem_ref_label(problem_id, problem_code)}: {e}"
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def tag_problem_task(
    self,
    problem_id=None,
    description="",
    problem_code=None,
):
    """
    Celery task to tag a problem (difficulty + types) using LLM.

    Args:
        problem_id: The problem's persistent database ID
        description: Optional description text from the edit form.
                     If provided, uses this instead of the DB description.
        problem_code: Legacy problem code for queued tasks created before the
            task argument was migrated to problem_id

    Returns:
        Dict with tagging results (stored in Celery result backend)
    """
    try:
        problem = _resolve_problem_for_ai_task(problem_id, problem_code)
        tag_service = get_problem_tag_service()
        result = tag_service.tag_single_problem(
            problem, description_override=description
        )

        # Convert type names to type objects for compatibility with both views
        predicted_types = []
        if result.get("predicted_types"):
            predicted_types = list(
                ProblemType.objects.filter(name__in=result["predicted_types"]).values(
                    "id", "name"
                )
            )

        return {
            "success": result["success"],
            "is_valid": result.get("is_valid", False),
            "predicted_points": result.get("predicted_points"),
            "predicted_types": predicted_types,
            "reason": result.get("reason"),
            "error": result.get("error"),
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "error": (
                f"Problem {_problem_ref_label(problem_id, problem_code)} not found"
            ),
        }

    except Exception as e:
        logger.error(
            f"Error in tag_problem_task for "
            f"{_problem_ref_label(problem_id, problem_code)}: {e}"
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def improve_markdown_task(
    self,
    problem_id=None,
    description="",
    problem_code=None,
):
    """
    Celery task to improve markdown formatting for a problem using LLM.

    Args:
        problem_id: The problem's persistent database ID
        description: Optional description text from the edit form.
                     If provided, uses this instead of the DB description.
        problem_code: Legacy problem code for queued tasks created before the
            task argument was migrated to problem_id

    Returns:
        Dict with improvement results (stored in Celery result backend)
    """
    try:
        problem = _resolve_problem_for_ai_task(problem_id, problem_code)
        tag_service = get_problem_tag_service()
        result = tag_service.improve_problem_markdown(
            problem, description_override=description
        )

        return {
            "success": result["success"],
            "improved_markdown": result.get("improved_markdown"),
            "error": result.get("error"),
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "error": (
                f"Problem {_problem_ref_label(problem_id, problem_code)} not found"
            ),
        }

    except Exception as e:
        logger.error(
            f"Error in improve_markdown_task for "
            f"{_problem_ref_label(problem_id, problem_code)}: {e}"
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def improve_question_markdown_task(self, content, choices_json=""):
    """
    Celery task to improve markdown formatting for a quiz question.

    Args:
        content: The raw question content text (not a model PK — works for both
                 edit and create pages since the object may not be saved yet)
        choices_json: Optional JSON string of answer choices to also improve

    Returns:
        Dict with improvement results (stored in Celery result backend)
    """
    try:
        service = get_quiz_ai_service()
        result = service.improve_question_markdown(content, choices_json)

        response = {
            "success": result["success"],
            "improved_markdown": result.get("improved_markdown"),
            "error": result.get("error"),
        }
        if result.get("improved_choices") is not None:
            response["improved_choices"] = result["improved_choices"]
        return response

    except Exception as e:
        logger.error(f"Error in improve_question_markdown_task: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def generate_question_explanation_task(
    self,
    question_content,
    question_type,
    choices_json,
    correct_answers_json,
    existing_explanation,
    rough_ideas="",
):
    """
    Celery task to generate or improve an explanation for a quiz question.

    Args:
        question_content: The question text
        question_type: Question type code (MC, MA, TF, SA, ES)
        choices_json: JSON string of choices list
        correct_answers_json: JSON string of correct answers
        existing_explanation: Existing explanation text (empty = generate new)

    Returns:
        Dict with generation results (stored in Celery result backend)
    """
    try:
        service = get_quiz_ai_service()
        result = service.generate_or_improve_explanation(
            question_content=question_content,
            question_type=question_type,
            choices_json=choices_json,
            correct_answers_json=correct_answers_json,
            existing_explanation=existing_explanation,
            rough_ideas=rough_ideas,
        )

        return {
            "success": result["success"],
            "explanation_content": result.get("explanation_content"),
            "mode": result.get("mode"),
            "error": result.get("error"),
        }

    except Exception as e:
        logger.error(f"Error in generate_question_explanation_task: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def generate_solution_codes_task(
    self,
    problem_id=None,
    model_id="",
    instructions="",
    include_reference=False,
    problem_code=None,
):
    """
    Celery task to generate multiple reference solution codes using LLM.

    Generates AC, WA, TLE solutions and appends them to existing
    ProblemSolutionCode entries.

    Args:
        problem_id: The problem's persistent database ID
        model_id: LLM model to use (empty = default)
        instructions: Optional user instructions for generation
        problem_code: Legacy problem code for queued tasks created before the
            task argument was migrated to problem_id

    Returns:
        Dict with {success, count, error}
    """
    try:
        problem = _resolve_problem_for_ai_task(problem_id, problem_code)
        config = get_config()
        bot_name = model_id or config.get_bot_name()

        generator = SolutionCodeGenerator(
            api_key=config.api_key,
            bot_name=bot_name,
            sleep_time=config.sleep_time,
        )
        result = generator.generate(
            problem, instructions=instructions, include_reference=include_reference
        )

        if not result["success"]:
            return {
                "success": False,
                "count": 0,
                "error": result.get("error", "Generation failed"),
            }

        solutions = result["solutions"]
        if not solutions:
            return {
                "success": False,
                "count": 0,
                "error": "LLM returned no valid solutions",
            }

        # Determine starting order (append after existing codes)
        max_order = problem.solution_codes.aggregate(max_order=Max("order"))[
            "max_order"
        ]
        next_order = (max_order or 0) + 1

        # Cache language lookups
        language_cache = {}
        created = 0

        for sol in solutions:
            lang_key = sol["language_key"]
            if lang_key not in language_cache:
                try:
                    language_cache[lang_key] = Language.objects.get(key=lang_key)
                except Language.DoesNotExist:
                    logger.warning(f"Language key '{lang_key}' not found, skipping")
                    continue

            ProblemSolutionCode.objects.create(
                problem=problem,
                order=next_order,
                name=sol["name"],
                source_code=sol["source_code"],
                language=language_cache[lang_key],
                expected_result=sol["expected_result"],
            )
            next_order += 1
            created += 1

        logger.info(f"Generated {created} solution codes for {problem.code}")
        return {
            "success": True,
            "count": created,
            "error": None,
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "count": 0,
            "error": (
                f"Problem {_problem_ref_label(problem_id, problem_code)} not found"
            ),
        }

    except Exception as e:
        logger.error(
            f"Error in generate_solution_codes_task for "
            f"{_problem_ref_label(problem_id, problem_code)}: {e}",
            exc_info=True,
        )
        return {
            "success": False,
            "count": 0,
            "error": str(e),
        }
