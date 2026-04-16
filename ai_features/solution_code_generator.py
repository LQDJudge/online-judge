"""
Solution code generator for competitive programming problems.
Uses LLM to generate multiple reference solutions (AC, WA, TLE) as structured JSON.
"""

import json
import re
import logging

from judge.models import Submission
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)

LANGUAGE_KEY_MAP = {
    "cpp": "CPP20",
    "c++": "CPP20",
    "python": "PYPY3",
    "pypy": "PYPY3",
    "pypy3": "PYPY3",
    "java": "JAVA",
}

SYSTEM_PROMPT = """You are an expert competitive programming solution writer.
Your task is to generate multiple reference solution codes for a problem.

ACT AS AN INDEPENDENT TESTER: Think about solutions yourself, like encountering
the problem for the first time. Write correct and incorrect solutions independently.

Generate solutions in these categories:
FOR C++ (language="cpp"):
a) Main AC solution — the intended optimal approach
b) Main AC solution in Python (language="python") — PyPy3
c) Alternative AC approach(es) if meaningfully different algorithms exist
d) Per-subtask AC solutions if IOI scoring with subtasks
e) WA codes (2-3) — common wrong approaches students might try:
   - Wrong greedy, missing edge cases, integer overflow, off-by-one
f) TLE code — correct brute-force but too slow for full constraints

CODE STYLE:
- Use `cin >> n;` directly — NEVER write defensive input checks
- Use `#include <bits/stdc++.h>` and `using namespace std;`
- NEVER use `typedef` or `#define` macros — write full type names
- Use clear, meaningful variable names

Use descriptive names like:
- "AC - Segment tree O(n log n)"
- "AC subtask 1,2 - Brute force O(n^2)"
- "WA - Greedy (wrong: doesn't handle negative values)"
- "TLE - O(n^3) triple loop (correct but slow)"

RESPONSE FORMAT: Return ONLY valid JSON in this exact format:
{"solutions": [
  {"name": "AC - description", "language": "cpp", "expected_result": "AC", "source_code": "..."},
  {"name": "AC - Python", "language": "python", "expected_result": "AC", "source_code": "..."},
  {"name": "WA - description", "language": "cpp", "expected_result": "WA", "source_code": "..."},
  {"name": "TLE - description", "language": "cpp", "expected_result": "TLE", "source_code": "..."}
]}

IMPORTANT:
- Return ONLY the JSON object, no markdown fences, no explanation before/after
- Each source_code must be a complete, compilable program
- expected_result must be one of: AC, WA, TLE, MLE, RTE
- language must be "cpp" or "python"
"""


class SolutionCodeGenerator:
    """Generates multiple reference solution codes for a problem using LLM."""

    def __init__(self, api_key, bot_name="Gemini-3-Flash", sleep_time=2.5):
        self.llm_service = LLMService(api_key, bot_name, sleep_time, timeout=300)
        self.bot_name = bot_name

    def generate(self, problem, instructions="", include_reference=False):
        """
        Generate solution codes for a problem.

        Args:
            problem: Problem model instance
            instructions: Optional user instructions for generation
            include_reference: If True, include editorial and AC submissions as context

        Returns:
            {"success": bool, "solutions": [...], "error": str|None}
        """
        try:
            user_prompt = self._build_prompt(problem, instructions, include_reference)
            response = self.llm_service.call_llm(
                prompt=user_prompt,
                system_prompt=SYSTEM_PROMPT,
            )

            if not response:
                return {
                    "success": False,
                    "solutions": [],
                    "error": f"No response from {self.bot_name} (model may be unavailable or timed out)",
                }

            solutions = self._parse_response(response)
            if not solutions:
                return {
                    "success": False,
                    "solutions": [],
                    "error": "Failed to parse solutions from LLM response",
                }

            return {
                "success": True,
                "solutions": solutions,
                "error": None,
            }

        except Exception as e:
            logger.error(f"Error generating solution codes: {e}", exc_info=True)
            return {
                "success": False,
                "solutions": [],
                "error": str(e),
            }

    def _build_prompt(self, problem, instructions="", include_reference=False):
        """Build the user prompt with problem context."""
        context = self._get_problem_context(problem)

        prompt = f"Generate reference solution codes for this problem:\n\n{context}"

        if include_reference:
            reference = self._get_reference_context(problem)
            if reference:
                prompt += f"\n\nREFERENCE MATERIAL (use as guidance, but still generate diverse WA/TLE codes independently):\n{reference}"

        if instructions:
            prompt += f"\n\nUSER INSTRUCTIONS:\n{instructions}"

        return prompt

    def _get_problem_context(self, problem):
        """Fetch problem statement and test data config for the prompt."""
        parts = []

        # Problem metadata
        scoring = (
            "IOI (partial points per subtask)"
            if problem.partial
            else "ICPC (all-or-nothing)"
        )
        parts.append(f"Problem: {problem.name} ({problem.code})")
        parts.append(f"Time Limit: {problem.time_limit}s")
        parts.append(f"Memory Limit: {problem.memory_limit} KB")
        parts.append(f"Scoring: {scoring}")
        parts.append("")

        # Problem statement
        if problem.description:
            desc = problem.description
            if len(desc) > 8000:
                desc = desc[:8000] + "\n\n... (truncated)"
            parts.append(f"PROBLEM STATEMENT:\n{desc}")
        else:
            parts.append("PROBLEM STATEMENT: (not available — uses PDF)")

        # Test data config (subtasks info)
        try:
            data = problem.data_files
            if data:
                cases = problem.cases.all().order_by("order")
                if cases.exists():
                    parts.append(f"\nTEST DATA ({cases.count()} cases):")
                    batch_num = 0
                    for case in cases:
                        type_map = {
                            "C": "Normal",
                            "S": "Batch Start",
                            "E": "Batch End",
                        }
                        case_type = type_map.get(case.type, case.type)
                        points = case.points if case.points is not None else "-"
                        if case.type == "S":
                            batch_num += 1
                        parts.append(f"  #{case.order}: {case_type}, points={points}")
                    if batch_num > 0:
                        parts.append(f"  Subtasks: {batch_num}")
        except Exception:
            pass

        return "\n".join(parts)

    def _get_reference_context(self, problem):
        """Fetch editorial and AC submissions as reference context."""
        parts = []

        # Editorial
        try:
            solution = problem.solution
            if solution and solution.content:
                content = solution.content
                if len(content) > 4000:
                    content = content[:4000] + "\n\n... (truncated)"
                parts.append(f"EDITORIAL:\n{content}")
        except Exception:
            pass

        # Author AC submissions
        for author in problem.authors.all()[:2]:
            sub = (
                Submission.objects.filter(problem=problem, user=author, result="AC")
                .order_by("-date")
                .first()
            )
            if sub:
                try:
                    source = sub.source.source
                    if len(source) > 4000:
                        source = source[:4000] + "\n... (truncated)"
                    lang = sub.language.name if sub.language else "Unknown"
                    parts.append(
                        f"AC SUBMISSION ({lang} by {author.user.username}):\n```\n{source}\n```"
                    )
                except Exception:
                    continue

        return "\n\n".join(parts) if parts else ""

    def _parse_response(self, response):
        """Parse JSON response from LLM, handling common formatting issues."""
        # Strip markdown code fences
        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Try to extract JSON object
        json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not json_match:
            logger.error("No JSON object found in response")
            logger.error(f"Response preview: {response[:500]}")
            return []

        json_str = json_match.group(0)

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            logger.error(f"JSON preview: {json_str[:500]}")
            return []

        if not isinstance(parsed, dict) or "solutions" not in parsed:
            logger.error("JSON missing 'solutions' key")
            return []

        # Validate and filter individual entries
        valid_results = {"AC", "WA", "TLE", "MLE", "RTE", "OLE", "IR"}
        valid_languages = set(LANGUAGE_KEY_MAP.keys())
        solutions = []

        for entry in parsed["solutions"]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "").strip()
            language = entry.get("language", "").strip().lower()
            expected_result = entry.get("expected_result", "").strip().upper()
            source_code = entry.get("source_code", "").strip()

            if not source_code:
                logger.warning(f"Skipping entry with empty source_code: {name}")
                continue
            if language not in valid_languages:
                logger.warning(
                    f"Skipping entry with invalid language '{language}': {name}"
                )
                continue
            if expected_result not in valid_results:
                logger.warning(
                    f"Skipping entry with invalid expected_result '{expected_result}': {name}"
                )
                continue

            solutions.append(
                {
                    "name": name[:128],
                    "language_key": LANGUAGE_KEY_MAP[language],
                    "expected_result": expected_result,
                    "source_code": source_code,
                }
            )

        logger.info(f"Parsed {len(solutions)} valid solutions from LLM response")
        return solutions
