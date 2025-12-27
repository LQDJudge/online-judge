"""
Solution/Editorial generator for problem descriptions
Uses LLM to generate solution explanations based on problem statement and AC code
"""

import time
from typing import Optional, Dict, Any
import logging
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


class SolutionGenerator:
    """Generates problem solutions/editorials using LLM"""

    def __init__(
        self, api_key: str, bot_name: str = "Claude-3.7-Sonnet", sleep_time: float = 2.5
    ):
        self.llm_service = LLMService(api_key, bot_name, sleep_time)
        self.sleep_time = sleep_time

    def _fix_unclosed_code_blocks(self, markdown: str) -> str:
        """
        Fix unclosed code blocks in markdown.
        Counts ``` occurrences and adds closing ``` if odd.
        """
        code_block_count = markdown.count("```")
        if code_block_count % 2 == 1:
            markdown = markdown.rstrip() + "\n```"
        return markdown

    def get_solution_template(self) -> str:
        """Return the target format template for solutions"""
        return """## Tóm tắt đề bài
Cho bốn số tự nhiên $a_1, b_1, a_2, b_2$ là kích thước của hai hình chữ nhật. Tìm diện tích hình vuông nhỏ nhất chứa được cả hai hình chữ nhật mà không xếp đè lên nhau.

## Phân tích
- **Điều kiện:** $0 < a_1, b_1, a_2, b_2 \\leq 10^6$
- **Độ phức tạp mục tiêu:** $O(1)$

## Hướng giải quyết

### Nhận xét
Để chứa hai hình chữ nhật trong một hình vuông, ta có thể sắp xếp chúng theo nhiều cách khác nhau. Vì mỗi hình có thể xoay 90 độ, ta cần xét tất cả các trường hợp có thể.

### Thuật toán
1. Thử tất cả các cách xoay của hai hình chữ nhật (4 trường hợp)
2. Với mỗi cách xoay, tính kích thước hình vuông tối thiểu khi xếp chúng:
   - Xếp chồng theo chiều dọc: cạnh = max(chiều rộng) và chiều cao tổng
   - Xếp cạnh nhau theo chiều ngang: chiều rộng tổng và max(chiều cao)
3. Chọn cách xếp cho hình vuông nhỏ nhất

## Độ phức tạp
- **Thời gian:** $O(1)$ - chỉ có một số lượng cố định các phép tính
- **Bộ nhớ:** $O(1)$

## Code tham khảo

```cpp
#include <bits/stdc++.h>
using namespace std;

int main() {
    int a1, b1, a2, b2;
    cin >> a1 >> b1 >> a2 >> b2;

    int ans = INT_MAX;
    // Thử tất cả các cách xoay
    for (int r1 = 0; r1 < 2; r1++) {
        for (int r2 = 0; r2 < 2; r2++) {
            int w1 = r1 ? b1 : a1, h1 = r1 ? a1 : b1;
            int w2 = r2 ? b2 : a2, h2 = r2 ? a2 : b2;

            // Xếp theo chiều dọc
            int side1 = max(max(w1, w2), h1 + h2);
            // Xếp theo chiều ngang
            int side2 = max(w1 + w2, max(h1, h2));

            ans = min(ans, min(side1 * side1, side2 * side2));
        }
    }

    cout << ans << endl;
    return 0;
}
```"""

    def _get_ac_solution(self, problem_obj) -> Optional[Dict[str, Any]]:
        """
        Get an accepted solution from the problem to help with solution generation.
        Returns dict with source code and language info, or None if not found.
        """
        if not problem_obj:
            return None

        try:
            # Import here to avoid circular imports
            from judge.models import Submission

            # First try to get solution from problem authors
            problem_authors = problem_obj.authors.all()

            # Get accepted submissions, prioritizing author submissions
            submissions_to_check = []

            if problem_authors.exists():
                # Get author submissions first
                for author in problem_authors:
                    author_submission = (
                        Submission.objects.filter(
                            problem=problem_obj, user=author, result="AC"
                        )
                        .order_by("-date")
                        .first()
                    )
                    if author_submission:
                        submissions_to_check.append(author_submission)

            # If no author submissions, get any AC submission
            if not submissions_to_check:
                any_ac = (
                    Submission.objects.filter(problem=problem_obj, result="AC")
                    .order_by("-date")
                    .first()
                )
                if any_ac:
                    submissions_to_check.append(any_ac)

            # Try to get source code from submissions
            for submission in submissions_to_check:
                try:
                    submission_source = submission.source.source
                    if submission_source:
                        # Get language info
                        language = (
                            submission.language.name
                            if submission.language
                            else "Unknown"
                        )

                        # Limit source code length
                        source = submission_source
                        if len(source) > 5000:
                            source = source[:5000] + "\n... (truncated)"

                        logger.info(
                            f"Found AC solution for {problem_obj.code} in {language}"
                        )
                        return {
                            "source": source,
                            "language": language,
                            "author": (
                                submission.user.username
                                if submission.user
                                else "Unknown"
                            ),
                        }
                except AttributeError:
                    continue

            logger.debug(f"No AC submissions found for problem {problem_obj.code}")
            return None

        except Exception as e:
            logger.error(f"Error getting AC solution for {problem_obj.code}: {e}")
            return None

    def generate_solution(
        self,
        problem_statement: str,
        problem_name: str = None,
        problem_code: str = None,
        problem_obj=None,
        rough_ideas: str = "",
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Generate a solution/editorial for a problem.

        Args:
            problem_statement: The problem statement (may include PDF reference)
            problem_name: Optional problem name for context
            problem_code: Optional problem code for context
            problem_obj: Optional Problem model instance to get AC code
            rough_ideas: Optional user-provided rough ideas or draft solution
            max_retries: Maximum number of retries

        Returns:
            Dict with 'success', 'solution_content', and optional 'error' fields
        """
        template = self.get_solution_template()

        # Get AC solution if problem object is provided
        ac_solution = self._get_ac_solution(problem_obj) if problem_obj else None

        system_prompt = f"""You are an expert competitive programming coach writing solution editorials.
Your task is to write clear, educational solution explanations for competitive programming problems.

IMPORTANT FORMATTING RULES:
1. Use Vietnamese language for the solution (unless the problem is in English)
2. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
3. Use ## for main section headers
4. Use ### for subsections
5. Structure the solution with these sections:
   - **Tóm tắt đề bài** (Problem Summary): Brief summary of what the problem asks
   - **Phân tích** (Analysis): Key constraints and observations
   - **Hướng giải quyết** (Approach): Step-by-step solution approach
   - **Độ phức tạp** (Complexity): Time and space complexity analysis
   - **Code tham khảo** (Reference Code): Clean, well-commented code

6. Keep explanations clear and educational
7. Explain the intuition behind the algorithm, not just the steps
8. Use bullet points and numbered lists for clarity
9. Include code with appropriate syntax highlighting (```cpp, ```python, etc.)

MARKDOWN INDENTATION AND LATEX RULES:
10. Sublist items MUST have 4 spaces indentation more than their parent item (NOT 2 spaces)
    Example:
    - Parent item
        - Child item (4 spaces before -)
            - Grandchild item (8 spaces before -)
11. Display LaTeX with $$...$$ MUST be a separate paragraph with a blank line above and below:
    Example:
    Some text here.

    $$x = \\\\frac{{-b \\\\pm \\\\sqrt{{b^2 - 4ac}}}}{{2a}}$$

    More text here.
12. For string literals, do NOT use LaTeX. Use `code` backticks or "raw" quotes instead:
    - WRONG: $"hello"$ or $\\\\text{{"hello"}}$
    - CORRECT: `hello` or "hello"

HERE IS THE TARGET FORMAT TEMPLATE:
{template}

CRITICAL INSTRUCTIONS:
- If an AC (Accepted) solution code is provided, analyze it and explain how it works
- Make the explanation accessible to intermediate competitive programmers
- Highlight key insights and common pitfalls
- If the problem has multiple approaches, mention the main one used in the code
- Output ONLY the solution markdown, no additional commentary"""

        problem_info = ""
        if problem_name or problem_code:
            problem_info = f"""PROBLEM INFO:
- Code: {problem_code or 'N/A'}
- Name: {problem_name or 'N/A'}

"""

        ac_code_section = ""
        if ac_solution:
            ac_code_section = f"""

AC SOLUTION CODE ({ac_solution['language']}):
```{ac_solution['language'].lower().split()[0]}
{ac_solution['source']}
```
"""

        rough_ideas_section = ""
        if rough_ideas:
            rough_ideas_section = f"""

USER'S ROUGH IDEAS/DRAFT:
The user has provided the following rough ideas or draft solution. Please improve and expand upon these ideas, maintaining their core approach while making the explanation clearer, more structured, and more educational:

{rough_ideas}

"""

        user_prompt = f"""{problem_info}Please write a solution editorial for the following competitive programming problem.
{ac_code_section}{rough_ideas_section}
PROBLEM STATEMENT:
{problem_statement}

OUTPUT: Provide ONLY the solution markdown in the format shown in the template. Use Vietnamese language."""

        for attempt in range(max_retries):
            logger.info(f"Solution generation attempt {attempt + 1}")

            response = self.llm_service.call_llm_with_files(
                user_prompt, problem_statement, system_prompt
            )

            if response:
                # Clean up the response
                solution = response.strip()

                # Remove ```markdown or ``` wrappers if present
                if solution.startswith("```markdown"):
                    solution = solution[11:]
                elif solution.startswith("```"):
                    solution = solution[3:]

                if solution.endswith("```"):
                    solution = solution[:-3]

                solution = solution.strip()

                # Fix unclosed code blocks
                solution = self._fix_unclosed_code_blocks(solution)

                if solution:
                    logger.info(f"Successfully generated solution for problem")
                    return {
                        "success": True,
                        "solution_content": solution,
                        "has_ac_code": ac_solution is not None,
                        "ac_language": ac_solution["language"] if ac_solution else None,
                    }
                else:
                    logger.warning("Empty response from LLM")
            else:
                logger.warning("Failed to get LLM response")

            # Delay before retrying
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {self.sleep_time} seconds...")
                time.sleep(self.sleep_time)

        return {
            "success": False,
            "solution_content": None,
            "error": "Failed to generate solution after all attempts",
        }
