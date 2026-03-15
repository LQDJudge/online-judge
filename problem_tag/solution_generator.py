"""
Solution/Editorial generator for problem descriptions
Uses LLM to generate solution explanations based on problem statement and AC code
"""

import time
from typing import Optional, Dict, Any
import logging
from llm_service.llm_api import LLMService
from llm_service.prompt_guidelines import get_markdown_rules_for_prompt

logger = logging.getLogger(__name__)


class SolutionGenerator:
    """Generates problem solutions/editorials using LLM"""

    def __init__(
        self, api_key: str, bot_name: str = "Claude-Sonnet-4.6", sleep_time: float = 2.5
    ):
        self.llm_service = LLMService(api_key, bot_name, sleep_time)
        self.sleep_time = sleep_time

    def _fix_latex(self, markdown: str) -> str:
        """
        Fix common LaTeX issues from LLM output.
        - Convert \\(...\\) to $...$
        - Convert \\[...\\] to $$...$$
        - Convert inline $$ to $ when used mid-paragraph
        """
        # Convert \(...\) to $...$ and \[...\] to $$...$$
        markdown = markdown.replace("\\(", "$").replace("\\)", "$")
        markdown = markdown.replace("\\[", "$$").replace("\\]", "$$")

        # Fix inline $$
        lines = markdown.split("\n")
        result = []
        for line in lines:
            stripped = line.strip()
            if "$$" in stripped:
                if not stripped.startswith("$$") or not stripped.endswith("$$"):
                    if stripped != "$$":
                        line = line.replace("$$", "$")
            result.append(line)
        return "\n".join(result)

    def _wrap_code_in_collapsible(self, markdown: str) -> str:
        """
        Convert ### C++ / ### Python headers followed by code blocks
        into collapsible ??? note sections for compact display.
        """
        import re

        def _indent(text, spaces=4):
            prefix = " " * spaces
            return "\n".join(
                prefix + line if line.strip() else line for line in text.split("\n")
            )

        # Match: ### C++ or ### Python (with optional whitespace) followed by a code block
        pattern = r"###\s*(C\+\+|Python)\s*\n+```(\w+)\n(.*?)```"

        def replacer(match):
            lang_label = match.group(1)  # "C++" or "Python"
            code_lang = match.group(2)  # "cpp" or "python"
            code = match.group(3).rstrip("\n")
            code_block = f"```{code_lang}\n{code}\n```"
            return f'??? note "{lang_label}"\n{_indent(code_block)}'

        return re.sub(pattern, replacer, markdown, flags=re.DOTALL)

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
Cho mảng gồm $n$ số nguyên $a_1, a_2, \\dots, a_n$. Tìm đoạn con liên tiếp có tổng lớn nhất.

## Phân tích
- **Điều kiện:** $1 \\leq n \\leq 10^6$, $|a_i| \\leq 10^9$
- **Nhận xét:** Cần xử lý cả số âm. Kết quả có thể là một phần tử duy nhất nếu toàn bộ mảng âm.

## Cách làm đơn giản (Brute Force)

### Ý tưởng
Duyệt tất cả các đoạn con $(i, j)$, tính tổng mỗi đoạn và lấy giá trị lớn nhất.

### Độ phức tạp
- **Thời gian:** $O(n^2)$
- **Đánh giá:** Phù hợp cho $n \\leq 5000$

### Code Brute Force

### C++
```cpp
#include <bits/stdc++.h>
using namespace std;
int main() {
    int n; cin >> n;
    vector<int> a(n);
    for (auto& x : a) cin >> x;
    long long ans = a[0];
    for (int i = 0; i < n; i++) {
        long long sum = 0;
        for (int j = i; j < n; j++) {
            sum += a[j];
            ans = max(ans, sum);
        }
    }
    cout << ans;
}
```

### Python
```python
n = int(input())
a = list(map(int, input().split()))
ans = a[0]
for i in range(n):
    total = 0
    for j in range(i, n):
        total += a[j]
        ans = max(ans, total)
print(ans)
```

## Hướng giải quyết (Tối ưu)

### Nhận xét
Nếu tổng đoạn hiện tại trở thành âm, ta bắt đầu đoạn mới từ phần tử tiếp theo (thuật toán Kadane).

### Thuật toán
1. Duy trì biến $cur$ là tổng đoạn con kết thúc tại vị trí hiện tại
2. Nếu $cur < 0$, đặt lại $cur = 0$ (bắt đầu đoạn mới)
3. Cập nhật kết quả $ans = \\max(ans, cur)$ sau mỗi bước

## Độ phức tạp
- **Thời gian:** $O(n)$
- **Bộ nhớ:** $O(1)$

## Code tham khảo

### C++
```cpp
#include <bits/stdc++.h>
using namespace std;
int main() {
    int n; cin >> n;
    vector<int> a(n);
    for (auto& x : a) cin >> x;
    long long ans = a[0], cur = 0;
    for (int i = 0; i < n; i++) {
        cur += a[i];
        ans = max(ans, cur);
        if (cur < 0) cur = 0;
    }
    cout << ans;
}
```

### Python
```python
n = int(input())
a = list(map(int, input().split()))
ans = a[0]
cur = 0
for x in a:
    cur += x
    ans = max(ans, cur)
    if cur < 0:
        cur = 0
print(ans)
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
        max_retries: int = 1,
    ) -> Dict[str, Any]:
        """
        Generate a solution/editorial for a problem.

        Args:
            problem_statement: The problem statement (may include PDF reference)
            problem_name: Optional problem name for context
            problem_code: Optional problem code for context
            problem_obj: Optional Problem model instance to get AC code
            rough_ideas: Optional user-provided rough ideas or draft solution
            max_retries: Maximum number of attempts

        Returns:
            Dict with 'success', 'solution_content', and optional 'error' fields
        """
        template = self.get_solution_template()

        # Get AC solution if problem object is provided
        ac_solution = self._get_ac_solution(problem_obj) if problem_obj else None
        markdown_rules = get_markdown_rules_for_prompt(start_number=10)

        system_prompt = f"""You are an expert competitive programming coach writing solution editorials for LQDOJ, a platform used by many beginners and students.
Your task is to write clear, educational solution explanations for competitive programming problems.

IMPORTANT FORMATTING RULES:
1. Use Vietnamese language for the solution (unless the problem is in English)
2. Use LaTeX math notation: single $ for inline math (e.g., $n$, $a_i$, $f(i) = f(i-1) + f(i-2)$). Use $$ ONLY for standalone equations on their own line. NEVER use $$ inside a paragraph — always use single $ for inline formulas, even long ones. NEVER use \\( \\) or \\[ \\] notation.
3. Use ## for main section headers
4. Use ### for subsections
5. Structure the solution with these sections IN ORDER:
   - **Tóm tắt đề bài** (Problem Summary): Brief summary of what the problem asks
   - **Phân tích** (Analysis): Key constraints and observations
   - **Cách làm đơn giản (Brute Force)** (ONLY if the problem is non-trivial): The simplest, most naive approach. Include: idea, complexity, and working code.
   - **Hướng giải quyết** (Approach / Optimal Approach): The efficient solution with step-by-step explanation. If there is a brute force section, label this "Hướng giải quyết (Tối ưu)".
   - **Độ phức tạp** (Complexity): Time and space complexity
   - **Code tham khảo** (Reference Code): Provide BOTH C++ and Python code

6. Keep explanations clear and educational
7. Explain the intuition behind the algorithm, not just the steps
8. Use bullet points and numbered lists for clarity
9. For ALL code sections (brute force and reference code), provide BOTH C++ and Python versions. Use this format:
   ### C++
   ```cpp
   // code here
   ```
   ### Python
   ```python
   # code here
   ```

CODE STYLE:
- Use `cin >> n;` directly — NEVER write `if (!(cin >> n)) return 0;` or similar defensive input checks
- Use `vector` or define arrays as global variables — NEVER define C-style arrays inside main() like `int arr[] = {...}`
- NEVER use `typedef` (e.g., `typedef long long ll`) or `#define` macros — write full type names so all readers can understand
- Use clear, meaningful variable names that help students understand the code
- Use `#include <bits/stdc++.h>` and `using namespace std;`

BRUTE FORCE SECTION GUIDELINES:
- SKIP this section ONLY for truly trivial problems where the solution is just a simple loop, if/else, or basic I/O with no algorithmic thinking needed (e.g., "print the sum of two numbers", "check if N is even").
- INCLUDE this section for ALL other problems, even if the optimal solution uses a formula or O(1) math. When the optimal solution requires a clever insight, pattern recognition, or mathematical formula, a brute force loop helps beginners VERIFY and UNDERSTAND what the formula computes. For example, if the answer is a formula, show the simple loop that iterates and counts/checks — this helps beginners see WHY the formula works.
- The brute force should be the MOST TRIVIAL approach: the first thing a beginner would think of (usually a simple for loop that simulates the problem directly)
- It should be simple enough for anyone who understands the problem to code
- Always include working brute force code (not pseudocode)
- Analyze its complexity and explain why it's too slow for full constraints
- Mention what partial score it could achieve (e.g., "Phù hợp cho $n \\leq 10^6$")
- This section bridges understanding the problem and understanding the optimal solution

{markdown_rules}

HERE IS THE TARGET FORMAT TEMPLATE:
{template}

CRITICAL INSTRUCTIONS:
- If an AC (Accepted) solution code is provided, analyze it and explain how it works
- If you include a brute force section, the brute force code must be DIFFERENT from the AC code - simpler and less efficient
- Make the explanation accessible to beginners and intermediate competitive programmers
- Highlight key insights and common pitfalls
- The editorial should flow: understand problem → simple approach (brute force) → optimize
- ONLY skip brute force for truly trivial problems (simple I/O, basic if/else)
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

                # Fix LaTeX notation issues
                solution = self._fix_latex(solution)

                # Convert "### C++" / "### Python" code sections to collapsible
                solution = self._wrap_code_in_collapsible(solution)

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
