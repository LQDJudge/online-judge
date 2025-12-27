"""
Markdown improver for problem descriptions
Uses LLM to format problem statements according to LQDOJ's markdown format
"""

import time
from typing import Dict, Any
import logging
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)


class MarkdownImprover:
    """Improves problem markdown formatting using LLM"""

    def __init__(
        self, api_key: str, bot_name: str = "Claude-3.7-Sonnet", sleep_time: float = 2.5
    ):
        self.llm_service = LLMService(api_key, bot_name, sleep_time)
        self.sleep_time = sleep_time

    def get_format_template(self) -> str:
        """Return the target format template for problem descriptions"""
        return """Cho bốn số tự nhiên $a_1, b_1, a_2, b_2$ với $(a_1, b_1)$ là độ dài các cạnh của hình chữ nhật thứ nhất và $(a_2, b_2)$ là độ dài các cạnh của hình chữ nhật thứ hai. Hãy đưa ra diện tích hình vuông nhỏ nhất chứa được cả hai hình chữ nhật này mà các hình chữ nhật không xếp đè lên nhau hoặc thừa ra bên ngoài hình vuông.

####Input
- Dữ liệu nhập vào từ bàn phím gồm bốn dòng lần lượt là bốn số tự nhiên $a_1, b_1, a_2, b_2 (0 < a_1, b_1, a_2, b_2\\leq 10^6)$.

####Output
- In ra màn hình một số duy nhất là diện tích của hình vuông bé nhất thoả mãn yêu cầu đề bài.

####Example

!!! question "Test 1"
    ???+ "Input"
        ```sample
        2
        3
        2
        4
        ```
    ???+ success "Output"
        ```sample
        16
        ```
    ??? warning "Note"
        Ta có hai hình chữ nhật kích thước là $2\\cdot 3$ và $2\\cdot 4$. Hai hình này đặt vừa trong hình vuông nhỏ nhất kích thước $4\\cdot 4$. Vậy cần đưa ra đáp số là $16$.
        1. ![markdown](https://i.imgur.com/YsozYqp.png)

!!! question "Test 2"
    ???+ "Input"
        ```sample
        4
        5
        4
        5
        ```
    ???+ success "Output"
        ```sample
        64
        ```
    ??? warning "Note"
        Với hai hình chữ nhật kích thước là $4\\cdot 5$ và $4\\cdot 5$ thì hình vuông nhỏ nhất chứa đủ phải có kích thước $8\\cdot 8$. Vậy cần đưa ra đáp số là $64$.

#### Scoring
 + Subtask $1$ ($20\\%$ số điểm): $n, k, x, |A_i|$ $\\le 10^3$ , $mod = 10^9 + 7.$
 + Subtask $2$ ($20\\%$ số điểm): $n$ và $k \\le 2.10^5 , |A_i|$ và $x \\le 10^9, mod = 10^9 + 7.$
 + Subtask $3$ ($60\\%$ số điểm): không có ràng buộc gì thêm."""

    def _fix_unclosed_code_blocks(self, markdown: str) -> str:
        """
        Fix unclosed code blocks in markdown.
        Counts ``` occurrences and adds closing ``` if odd.
        """
        # Count code block markers
        code_block_count = markdown.count("```")

        # If odd number, add closing ```
        if code_block_count % 2 == 1:
            markdown = markdown.rstrip() + "\n        ```"

        return markdown

    def improve_markdown(
        self,
        problem_statement: str,
        problem_name: str = None,
        problem_code: str = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Improve the markdown formatting of a problem statement.

        Args:
            problem_statement: The original problem statement (may include PDF reference)
            problem_name: Optional problem name for context
            problem_code: Optional problem code for context
            max_retries: Maximum number of retries

        Returns:
            Dict with 'success', 'improved_markdown', and optional 'error' fields
        """
        template = self.get_format_template()

        system_prompt = f"""You are an expert at formatting competitive programming problem statements.
Your task is to convert problem statements to a specific LQDOJ markdown format.

IMPORTANT FORMATTING RULES:
1. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
2. Use #### for section headers (####Input, ####Output, ####Example)
3. Format constraints in bullet points with - prefix
4. Use the special admonition syntax for test cases:
   - !!! question "Test X" for each test case
   - ???+ "Input" with ```sample code block for input
   - ???+ success "Output" with ```sample code block for output
   - ??? warning "Note" for optional explanations (note: ??? without + means collapsed by default)
5. Keep all images as markdown links: ![description](url)
6. Preserve the original language (Vietnamese or English)
7. Use \\cdot for multiplication in math mode
8. Format large numbers with scientific notation where appropriate (e.g., $10^9$)

MARKDOWN INDENTATION AND LATEX RULES:
9. Sublist items MUST have 4 spaces indentation more than their parent item (NOT 2 spaces)
   Example:
   - Parent item
       - Child item (4 spaces before -)
           - Grandchild item (8 spaces before -)
10. Display LaTeX with $$...$$ MUST be a separate paragraph with a blank line above and below:
    Example:
    Some text here.

    $$x = \\\\frac{{-b \\\\pm \\\\sqrt{{b^2 - 4ac}}}}{{2a}}$$

    More text here.
11. For string literals, do NOT use LaTeX. Use `code` backticks or "raw" quotes instead:
    - WRONG: $"hello"$ or $\\\\text{{"hello"}}$
    - CORRECT: `hello` or "hello"

HERE IS THE TARGET FORMAT TEMPLATE:
{template}

CRITICAL INSTRUCTIONS:
- Analyze any PDF attachments carefully - they often contain the complete problem statement
- Extract ALL information: problem description, constraints, input/output format, examples, and notes
- Maintain the EXACT same content/meaning, just reformat to match the template style
- If there are multiple test cases, create separate !!! question blocks for each
- Do NOT add or remove any information, only reformat
- Output ONLY the improved markdown, no explanations or commentary"""

        problem_info = ""
        if problem_name or problem_code:
            problem_info = f"""PROBLEM INFO:
- Code: {problem_code or 'N/A'}
- Name: {problem_name or 'N/A'}

"""

        user_prompt = f"""{problem_info}Please convert the following problem statement to the LQDOJ markdown format.
If the content includes PDF references/attachments, analyze the PDF content and extract the problem statement from it.

ORIGINAL PROBLEM STATEMENT:
{problem_statement}

OUTPUT: Provide ONLY the reformatted markdown, nothing else."""

        for attempt in range(max_retries):
            logger.info(f"Markdown improvement attempt {attempt + 1}")

            response = self.llm_service.call_llm_with_files(
                user_prompt, problem_statement, system_prompt
            )

            if response:
                # Clean up the response - remove any markdown code block wrappers
                improved = response.strip()

                # Remove ```markdown or ``` wrappers if present
                if improved.startswith("```markdown"):
                    improved = improved[11:]
                elif improved.startswith("```"):
                    improved = improved[3:]

                if improved.endswith("```"):
                    improved = improved[:-3]

                improved = improved.strip()

                # Fix unclosed code blocks
                improved = self._fix_unclosed_code_blocks(improved)

                if improved:
                    logger.info(f"Successfully improved markdown for problem")
                    return {
                        "success": True,
                        "improved_markdown": improved,
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
            "improved_markdown": None,
            "error": "Failed to improve markdown after all attempts",
        }
