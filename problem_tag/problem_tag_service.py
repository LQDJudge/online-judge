"""
Problem tag service for LQDOJ
Functions to work with LQDOJ Problem models for tagging and difficulty prediction
"""

import os
import time
from typing import List, Dict, Optional
from .problem_tagger import ProblemTagger
from .markdown_improver import MarkdownImprover
from .solution_generator import SolutionGenerator
from llm_service.llm_api import LLMService
from llm_service.config import get_config
import logging

logger = logging.getLogger(__name__)


class ProblemTagService:
    """Service for tagging problems with difficulty and types using LLM"""

    def __init__(self):
        self.config = get_config()
        self.tagger = ProblemTagger(
            api_key=self.config.api_key,
            bot_name=self.config.bot_name,
            sleep_time=self.config.sleep_time,
        )
        self.llm_service = LLMService(
            api_key=self.config.api_key,
            bot_name=self.config.bot_name,
            sleep_time=self.config.sleep_time,
        )
        self.markdown_improver = MarkdownImprover(
            api_key=self.config.api_key,
            bot_name=self.config.bot_name,
            sleep_time=self.config.sleep_time,
        )
        self.solution_generator = SolutionGenerator(
            api_key=self.config.api_key,
            bot_name=self.config.bot_name,
            sleep_time=self.config.sleep_time,
        )

    def get_problem_statement(self, problem) -> Optional[str]:
        """
        Get problem statement from Django Problem model (text description + PDF reference)

        Args:
            problem: Django Problem model instance

        Returns:
            Problem statement as markdown string (with PDF reference if available), or None if not available
        """
        try:
            statement_parts = []

            # Add text description if available
            if problem.description:
                statement_parts.append(problem.description)

            # Add PDF reference if available
            if problem.pdf_description and problem.pdf_description.name:
                pdf_url = problem.pdf_description.url
                pdf_name = os.path.basename(problem.pdf_description.name)
                pdf_markdown = f"[PDF Statement: {pdf_name}]({pdf_url})"
                statement_parts.append(pdf_markdown)

            if statement_parts:
                return "\n\n".join(statement_parts)
            else:
                return None

        except Exception as e:
            logger.error(f"Error getting statement for problem {problem.code}: {e}")
            return None

    def get_available_tags(self) -> List[str]:
        """
        Get list of available problem types (tags) from Django models

        Returns:
            List of type names
        """
        try:
            # Import here to avoid circular imports
            from judge.models import ProblemType

            return list(ProblemType.objects.values_list("name", flat=True))
        except Exception as e:
            logger.error(f"Error getting available types: {e}")
            return []

    def call_llm_api(self, prompt: str, system_prompt: str = None) -> Optional[str]:
        """
        Direct call to LLM API for general tasks
        """
        return self.llm_service.call_llm(prompt, system_prompt)

    def tag_single_problem(self, problem) -> Dict[str, any]:
        """
        Tag a single problem using LLM (format validation + difficulty + tags in one call)

        Args:
            problem: Django Problem model instance

        Returns:
            Dict with tagging results
        """
        statement = self.get_problem_statement(problem)
        if not statement:
            return {
                "problem_code": problem.code,
                "success": False,
                "is_valid": False,
                "error": "Could not extract problem statement",
            }

        available_tags = self.get_available_tags()
        if not available_tags:
            return {
                "problem_code": problem.code,
                "success": False,
                "is_valid": False,
                "error": "No available types found",
            }

        try:
            # Unified analysis: format validation + difficulty + tags
            result = self.tagger.analyze_and_tag_problem(
                statement,
                available_tags,
                problem_obj=problem,
                max_retries=self.config.max_retries,
            )

            return {
                "problem_code": problem.code,
                "success": True,
                "is_valid": result["is_valid"],
                "predicted_points": result["points"],  # points field in Problem model
                "predicted_types": result["tags"],  # types field in Problem model
                "statement_length": len(statement),
                "reason": result.get("reason"),  # Reason if is_valid is False
            }
        except Exception as e:
            logger.error(f"Error tagging problem {problem.code}: {e}")
            return {
                "problem_code": problem.code,
                "success": False,
                "is_valid": False,
                "error": str(e),
            }

    def improve_problem_markdown(self, problem) -> Dict[str, any]:
        """
        Improve the markdown formatting of a problem's description using LLM

        Args:
            problem: Django Problem model instance

        Returns:
            Dict with improvement results including 'success' and 'improved_markdown'
        """
        statement = self.get_problem_statement(problem)
        if not statement:
            return {
                "problem_code": problem.code,
                "success": False,
                "error": "Could not extract problem statement",
            }

        try:
            result = self.markdown_improver.improve_markdown(
                problem_statement=statement,
                problem_name=problem.name,
                problem_code=problem.code,
                max_retries=self.config.max_retries,
            )

            return {
                "problem_code": problem.code,
                "success": result["success"],
                "improved_markdown": result.get("improved_markdown"),
                "error": result.get("error"),
            }
        except Exception as e:
            logger.error(f"Error improving markdown for problem {problem.code}: {e}")
            return {
                "problem_code": problem.code,
                "success": False,
                "error": str(e),
            }

    def generate_problem_solution(
        self, problem, rough_ideas: str = ""
    ) -> Dict[str, any]:
        """
        Generate a solution/editorial for a problem using LLM

        Args:
            problem: Django Problem model instance
            rough_ideas: Optional user-provided rough ideas or draft solution

        Returns:
            Dict with generation results including 'success' and 'solution_content'
        """
        statement = self.get_problem_statement(problem)
        if not statement:
            return {
                "problem_code": problem.code,
                "success": False,
                "error": "Could not extract problem statement",
            }

        try:
            result = self.solution_generator.generate_solution(
                problem_statement=statement,
                problem_name=problem.name,
                problem_code=problem.code,
                problem_obj=problem,
                rough_ideas=rough_ideas,
                max_retries=self.config.max_retries,
            )

            return {
                "problem_code": problem.code,
                "success": result["success"],
                "solution_content": result.get("solution_content"),
                "has_ac_code": result.get("has_ac_code", False),
                "ac_language": result.get("ac_language"),
                "error": result.get("error"),
            }
        except Exception as e:
            logger.error(f"Error generating solution for problem {problem.code}: {e}")
            return {
                "problem_code": problem.code,
                "success": False,
                "error": str(e),
            }

    def tag_problem_batch(self, problem_codes: List[str]) -> List[Dict[str, any]]:
        """
        Tag a batch of problems by their codes

        Args:
            problem_codes: List of problem codes to tag

        Returns:
            List of tagging results
        """
        try:
            # Import here to avoid circular imports
            from judge.models import Problem

            problems = Problem.objects.filter(code__in=problem_codes)
            results = []

            for problem in problems:
                result = self.tag_single_problem(problem)
                results.append(result)

                # Sleep between requests to respect rate limits
                time.sleep(self.config.sleep_time)

            return results
        except Exception as e:
            logger.error(f"Error in batch tagging: {e}")
            return []

    def update_problem_with_tags(
        self,
        problem,
        tag_result: Dict[str, any],
        update_points: bool = True,
        update_types: bool = True,
    ) -> bool:
        """
        Update Django Problem model with LLM tagging results (only if is_valid is True)

        Args:
            problem: Django Problem model instance
            tag_result: Result from tag_single_problem
            update_points: Whether to update the points field
            update_types: Whether to update the types field

        Returns:
            True if successful, False otherwise
        """
        if not tag_result.get("success"):
            logger.warning(f"Cannot update problem {problem.code}: tagging failed")
            return False

        if not tag_result.get("is_valid"):
            logger.info(
                f"Skipping problem {problem.code}: invalid format - cannot determine accurate difficulty/tags"
            )
            return False

        try:
            # Import here to avoid circular imports
            from judge.models import ProblemType

            updated = False

            # Update points (difficulty) if available and requested
            predicted_points = tag_result.get("predicted_points")
            points_updated = False
            if update_points and predicted_points is not None and predicted_points > 0:
                problem.points = float(predicted_points)
                updated = True
                points_updated = True
                logger.info(
                    f"Updated problem {problem.code} points to {predicted_points}"
                )

            # Update types if available and requested
            predicted_types = tag_result.get("predicted_types", [])
            if update_types and predicted_types:
                # Get ProblemType objects for the predicted types
                type_objects = ProblemType.objects.filter(name__in=predicted_types)

                if type_objects.exists():
                    # Add types to problem (don't clear existing ones)
                    for type_obj in type_objects:
                        problem.types.add(type_obj)
                    updated = True
                    logger.info(
                        f"Updated problem {problem.code} with {len(type_objects)} types"
                    )

            if updated:
                problem.save()

                # If points were updated, trigger rescore task to update all submission points
                if points_updated:
                    from judge.tasks.submission import rescore_problem

                    rescore_problem.delay(problem.id)
                    logger.info(f"Triggered rescore task for problem {problem.code}")

                return True
            else:
                logger.warning(f"No updates made to problem {problem.code}")
                return False

        except Exception as e:
            logger.error(f"Error updating problem {problem.code}: {e}")
            return False

    def get_problems_by_codes(self, codes: List[str]):
        """
        Get problems by their codes

        Args:
            codes: List of problem codes

        Returns:
            QuerySet of Problem objects
        """
        try:
            # Import here to avoid circular imports
            from judge.models import Problem

            return Problem.objects.filter(code__in=codes)
        except Exception as e:
            logger.error(f"Error getting problems by codes: {e}")
            return []


# Global instance
_problem_tag_service = None


def get_problem_tag_service() -> ProblemTagService:
    """Get global Problem Tag Service instance"""
    global _problem_tag_service
    if _problem_tag_service is None:
        _problem_tag_service = ProblemTagService()
    return _problem_tag_service
