"""
Problem tagger for difficulty and tag prediction
Uses general LLM API for all LLM interactions
"""

import json
import re
import time
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any
import logging
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


class ProblemTagger:
    """Tagger for predicting problem difficulty and tags using LLM with format validation"""

    def __init__(
        self, api_key: str, bot_name: str = "Claude-3.7-Sonnet", sleep_time: float = 2.5
    ):
        self.llm_service = LLMService(api_key, bot_name, sleep_time)
        self.sleep_time = sleep_time

    def parse_json_response(self, response: str) -> Dict[str, Any]:
        """
        Parse JSON response from the LLM.
        Expected format: {"is_valid": true/false, "points": 1500, "tags": ["tag1", "tag2"], "reason": "explanation_if_invalid"}
        """
        try:
            # Try to extract JSON from response
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)

                # Try parsing as-is first
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    # Fix common issues: single quotes -> double quotes
                    # Use Python's ast.literal_eval for Python-style dicts
                    import ast

                    try:
                        # Try parsing as Python literal (handles single quotes, True/False, None)
                        python_obj = ast.literal_eval(json_str)
                        # Convert to proper JSON types
                        parsed = json.loads(json.dumps(python_obj))
                        logger.debug("Parsed using ast.literal_eval")
                    except (ValueError, SyntaxError):
                        # Fallback: manual regex fixes
                        fixed_str = json_str
                        # Replace single quotes with double quotes
                        fixed_str = re.sub(
                            r"'(\w+)'(\s*:)", r'"\1"\2', fixed_str
                        )  # 'key': -> "key":
                        fixed_str = re.sub(
                            r":\s*'([^']*)'", r': "\1"', fixed_str
                        )  # : 'value' -> : "value"
                        fixed_str = re.sub(r"\[\s*'", r'["', fixed_str)  # [' -> ["
                        fixed_str = re.sub(r"'\s*\]", r'"]', fixed_str)  # '] -> "]
                        fixed_str = re.sub(
                            r"'\s*,\s*'", r'", "', fixed_str
                        )  # ', ' -> ", "

                        logger.debug(f"Fixed JSON string: {fixed_str[:200]}...")
                        parsed = json.loads(fixed_str)

                # Validate required fields
                if not isinstance(parsed, dict):
                    raise ValueError("Response is not a JSON object")

                # Set defaults for missing fields
                result = {
                    "is_valid": parsed.get("is_valid", False),
                    "points": parsed.get("points"),
                    "tags": parsed.get("tags", []),
                    "reason": parsed.get("reason"),
                }

                # Validate types
                if not isinstance(result["is_valid"], bool):
                    result["is_valid"] = False

                if result["points"] is not None and not isinstance(
                    result["points"], (int, float)
                ):
                    result["points"] = None

                if not isinstance(result["tags"], list):
                    result["tags"] = []

                if result["reason"] is not None and not isinstance(
                    result["reason"], str
                ):
                    result["reason"] = str(result["reason"])

                return result
            else:
                raise ValueError("No JSON found in response")

        except Exception as e:
            # Log the raw response for debugging
            response_preview = response[:500] if response else "(empty)"
            logger.error(f"Error parsing JSON response: {e}")
            logger.error(f"Raw response preview: {response_preview}")
            return {
                "is_valid": False,
                "points": None,
                "tags": [],
                "reason": f"Parse error: {e}",
            }

    def _get_author_solution(self, problem_obj) -> Optional[str]:
        """
        Get an accepted solution from the problem author to help with analysis.
        Returns the source code of the submission, or None if not found.
        """
        if not problem_obj:
            return None

        try:
            # Import here to avoid circular imports
            from judge.models import Submission

            # Get problem authors (usually the problem creator)
            problem_authors = problem_obj.authors.all()
            if not problem_authors.exists():
                logger.debug(f"No authors found for problem {problem_obj.code}")
                return None

            # Find an accepted submission from any of the authors
            large_sources = []
            for author in problem_authors:
                accepted_submission = (
                    Submission.objects.filter(
                        problem=problem_obj, user=author, result="AC"  # Accepted
                    )
                    .order_by("-date")
                    .first()
                )  # Get most recent accepted submission

                if accepted_submission:
                    # Get the actual source code (it's in a related SubmissionSource object)
                    try:
                        submission_source = accepted_submission.source.source
                        if submission_source:
                            logger.info(
                                f"Found author solution for {problem_obj.code} by {author.username}"
                            )
                            # Limit source code length to avoid very long prompts
                            source = submission_source
                            if len(source) > 3000:  # Limit to ~3000 characters
                                source = source[:3000] + "\n... (truncated)"
                                large_sources.append(source)
                            else:
                                return source
                    except AttributeError:
                        # No source object or source field
                        logger.debug(
                            f"No source found for submission {accepted_submission.id}"
                        )
                        continue

            if large_sources:
                return large_sources[0]

            logger.debug(
                f"No accepted submissions found from authors for problem {problem_obj.code}"
            )
            return None

        except Exception as e:
            logger.error(f"Error getting author solution for {problem_obj.code}: {e}")
            return None

    def analyze_and_tag_problem(
        self,
        problem_statement: str,
        available_tags: List[str],
        problem_obj=None,
        max_retries: int = 1,
    ) -> Dict[str, Any]:
        """
        Analyze problem format, predict difficulty and tags in one unified call.
        Returns: {"is_valid": bool, "points": int, "tags": [str]} or {"is_valid": False, ...} if failed
        """
        tags_str = ", ".join(available_tags)

        system_prompt = f"""You are an expert competitive programming judge with deep knowledge of algorithmic problems and contest difficulty ratings.

AVAILABLE TAGS (use ONLY these exact tags): {tags_str}

DIFFICULTY RATING GUIDELINES (like Codeforces):
- 800-1200: Basic implementation, simple math, greedy, ad-hoc
- 1300-1600: Standard algorithms, basic DP, graph traversal, binary search  
- 1700-2000: Advanced algorithms, complex DP, data structures, number theory
- 2100-2400: Sophisticated techniques, advanced data structures, complex math
- 2500+: Expert-level algorithms, research-level techniques

TAG SELECTION RULES:
1. Choose 1-4 most relevant tags that represent the CORE techniques needed
2. Focus on the PRIMARY algorithmic approach, not auxiliary operations  
3. Avoid basic operations (sorting, I/O) unless they're the main challenge
4. Use specific tags over general ones when available
5. For mixed problems, include all essential techniques

IMPORTANT: If files (images, PDFs, etc.) are provided as attachments, analyze them carefully as they may contain the complete problem statement, constraints, examples, diagrams, or additional context that are essential for understanding the problem requirements.

MULTI-PROBLEM FILES: If a file contains multiple problems (like a contest problem set), use the problem name and code provided to identify and analyze ONLY the specific problem requested. Focus on the problem that matches the given name/code, and ignore other problems in the same file."""

        # Get author's accepted submission to help with analysis
        author_solution = (
            self._get_author_solution(problem_obj) if problem_obj else None
        )

        if author_solution:
            problem_info = ""
            if problem_obj:
                problem_info = f"""PROBLEM TO ANALYZE:
- Problem Code: {problem_obj.code}
- Problem Name: {problem_obj.name}

"""

            user_prompt = f"""TASK: Analyze this competitive programming problem and provide:
1. Format validation (does it have complete problem statement, input/output format, constraints, examples?)
2. Difficulty rating (integer, like Codeforces rating) - only if valid format
3. Core algorithmic tags (1-4 tags from provided list) - only if valid format

{problem_info}You have both the problem statement and author's accepted solution. Problem statement is in Vietnamese or English.
If files (images, PDFs, etc.) are provided as attachments, they may contain the complete problem description, so analyze them carefully. If the file contains multiple problems, focus ONLY on the problem that matches the code and name above.

RESPONSE FORMAT: Return ONLY valid JSON in this exact format:
{{"is_valid": true/false, "points": difficulty_rating_or_null, "tags": ["tag1", "tag2"], "reason": "explanation_if_invalid" }}

If is_valid is false, set points to null, tags to empty array, and provide a clear reason explaining what's missing or incomplete in the problem statement.
If is_valid is true, analyze the solution approach and provide accurate difficulty and tags. Set reason to null.

PROBLEM STATEMENT:
{problem_statement}

AUTHOR'S ACCEPTED SOLUTION:
{author_solution}"""
        else:
            problem_info = ""
            if problem_obj:
                problem_info = f"""PROBLEM TO ANALYZE:
- Problem Code: {problem_obj.code}
- Problem Name: {problem_obj.name}

"""

            user_prompt = f"""TASK: Analyze this competitive programming problem and provide:
1. Format validation (does it have complete problem statement, input/output format, constraints, examples?)
2. Difficulty rating (integer, like Codeforces rating) - only if valid format  
3. Core algorithmic tags (1-4 tags from provided list) - only if valid format

{problem_info}Problem statement is in Vietnamese or English.
If files (images, PDFs, etc.) are provided as attachments, they may contain the complete problem description, so analyze them carefully. If the file contains multiple problems, focus ONLY on the problem that matches the code and name above.

RESPONSE FORMAT: Return ONLY valid JSON in this exact format:
{{"is_valid": true/false, "points": difficulty_rating_or_null, "tags": ["tag1", "tag2"], "reason": "explanation_if_invalid" }}

If is_valid is false, set points to null, tags to empty array, and provide a clear reason explaining what's missing or incomplete in the problem statement.
If is_valid is true, provide accurate difficulty and tags based on the problem requirements. Set reason to null.

PROBLEM STATEMENT:
{problem_statement}"""

        for attempt in range(max_retries):
            logger.info(f"Problem tagging attempt {attempt + 1}")
            response = self.llm_service.call_llm_with_files(
                user_prompt, problem_statement, system_prompt
            )

            if response:
                parsed_result = self.parse_json_response(response)

                # If we got a valid result, return it
                if parsed_result["is_valid"]:
                    logger.info(f"Analysis result: {parsed_result}")
                    return parsed_result
                else:
                    # If is_valid is false, do not retry and print the reason
                    reason = parsed_result.get("reason", "No reason provided")
                    logger.warning(f"Problem format is invalid. Reason: {reason}")
                    return parsed_result
            else:
                logger.warning("Failed to get a valid LLM response")

            # Delay before retrying (only reached if no response)
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {self.sleep_time} seconds...")
                time.sleep(self.sleep_time)

        # Return invalid result if all retries fail (only happens if no response at all)
        return {
            "is_valid": False,
            "points": None,
            "tags": [],
            "reason": "Failed to get valid LLM response after all attempts",
        }

    def process_problem_batch(
        self,
        problems: List[Tuple[str, str]],
        available_tags: List[str],
        output_file: str = "predictions.txt",
        error_log_file: str = "log_errors.txt",
    ) -> List[Tuple[str, Optional[int], List[str]]]:
        """
        Process a batch of problems and return results.

        Args:
            problems: List of (problem_code, problem_description) tuples
            available_tags: List of valid tags to use
            output_file: File to save results
            error_log_file: File to log errors

        Returns:
            List of (problem_code, difficulty, tags) tuples
        """
        results = []

        for code, description in problems:
            # Use the analyze_and_tag_problem method
            result = self.analyze_and_tag_problem(description, available_tags)
            difficulty = result.get("points")
            tags = result.get("tags", [])
            results.append((code, difficulty, tags))

            # Log errors if prediction failed
            if not result.get("success", False):
                with open(error_log_file, "a", encoding="utf-8") as log_file:
                    log_file.write(
                        f"[{datetime.now()}] Failed to analyze problem {code}\n"
                    )

            # Sleep between requests to respect rate limits
            time.sleep(self.sleep_time)

        # Save results to file
        try:
            with open(output_file, "w", encoding="utf-8") as file:
                for result in results:
                    file.write(f"{result}\n")
            logger.info(f"Results saved to {output_file}")
        except Exception as e:
            logger.error(f"Error writing to {output_file}: {e}")

        return results
