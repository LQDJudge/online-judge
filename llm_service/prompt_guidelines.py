"""
Shared prompt guidelines for LLM services.
Contains common formatting rules for markdown, LaTeX, etc.
"""

MARKDOWN_FORMATTING_RULES = """
MARKDOWN INDENTATION AND LATEX RULES:
1. Sublist items MUST have 4 spaces indentation more than their parent item (NOT 2 spaces)
   Example:
   - Parent item
       - Child item (4 spaces before -)
           - Grandchild item (8 spaces before -)

2. Display LaTeX (block equations) - TWO OPTIONS:
   Option A: Use $$...$$ as a SEPARATE PARAGRAPH with blank lines above and below:
   Some text here.

   $$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$

   More text here.

   Option B (PREFERRED for equations inside lists): Use ```math fence block:
   - List item with equation:

       ```math
       x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}
       ```

   - Next list item continues...

   NOTE: Option B keeps the equation inside the list structure. Use 4-space indent for the fence block.

3. For string literals, do NOT use LaTeX. Use `code` backticks or "raw" quotes instead:
   - WRONG: $"hello"$ or $\\text{"hello"}$
   - CORRECT: `hello` or "hello"
""".strip()

LATEX_BASIC_RULES = """
1. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
2. Use \\cdot for multiplication in math mode
3. Format large numbers with scientific notation where appropriate (e.g., $10^9$)
""".strip()


def get_markdown_rules_for_prompt(start_number: int = 1) -> str:
    """
    Get markdown formatting rules with customizable starting number.

    Args:
        start_number: The starting rule number (default 1)

    Returns:
        Formatted markdown rules string with adjusted numbering
    """
    # Replace rule numbers dynamically
    rules = MARKDOWN_FORMATTING_RULES
    rules = rules.replace(
        "MARKDOWN INDENTATION AND LATEX RULES:\n1.",
        f"MARKDOWN INDENTATION AND LATEX RULES:\n{start_number}.",
    )
    rules = rules.replace("\n2. Display LaTeX", f"\n{start_number + 1}. Display LaTeX")
    rules = rules.replace(
        "\n3. For string literals", f"\n{start_number + 2}. For string literals"
    )
    return rules
