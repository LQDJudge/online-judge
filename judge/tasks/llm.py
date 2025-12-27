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
