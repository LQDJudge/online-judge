"""
Celery tasks for LLM operations (solution generation, markdown improvement, etc.)
These tasks run asynchronously to avoid timeout issues with long-running LLM calls.
"""

from celery import shared_task

import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def generate_solution_task(self, problem_code, rough_ideas=""):
    """
    Celery task to generate solution for a problem using LLM.

    Args:
        problem_code: The problem code
        rough_ideas: Optional user-provided rough ideas

    Returns:
        Dict with generation results (stored in Celery result backend)
    """
    try:
        # Import here to avoid circular imports
        from judge.models import Problem
        from problem_tag.problem_tag_service import get_problem_tag_service

        problem = Problem.objects.get(code=problem_code)
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
            "error": f"Problem {problem_code} not found",
        }

    except Exception as e:
        logger.error(f"Error in generate_solution_task for {problem_code}: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def tag_problem_task(self, problem_code):
    """
    Celery task to tag a problem (difficulty + types) using LLM.

    Args:
        problem_code: The problem code

    Returns:
        Dict with tagging results (stored in Celery result backend)
    """
    try:
        # Import here to avoid circular imports
        from judge.models import Problem, ProblemType
        from problem_tag.problem_tag_service import get_problem_tag_service

        problem = Problem.objects.get(code=problem_code)
        tag_service = get_problem_tag_service()
        result = tag_service.tag_single_problem(problem)

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
            "error": f"Problem {problem_code} not found",
        }

    except Exception as e:
        logger.error(f"Error in tag_problem_task for {problem_code}: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(bind=True)
def improve_markdown_task(self, problem_code):
    """
    Celery task to improve markdown formatting for a problem using LLM.

    Args:
        problem_code: The problem code

    Returns:
        Dict with improvement results (stored in Celery result backend)
    """
    try:
        # Import here to avoid circular imports
        from judge.models import Problem
        from problem_tag.problem_tag_service import get_problem_tag_service

        problem = Problem.objects.get(code=problem_code)
        tag_service = get_problem_tag_service()
        result = tag_service.improve_problem_markdown(problem)

        return {
            "success": result["success"],
            "improved_markdown": result.get("improved_markdown"),
            "error": result.get("error"),
        }

    except Problem.DoesNotExist:
        return {
            "success": False,
            "error": f"Problem {problem_code} not found",
        }

    except Exception as e:
        logger.error(f"Error in improve_markdown_task for {problem_code}: {e}")
        return {
            "success": False,
            "error": str(e),
        }
