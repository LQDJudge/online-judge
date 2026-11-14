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

4. Inline LaTeX MUST NOT have spaces after opening $ or before closing $:
   - WRONG: $ f_i = $ or $f_i = $ (spaces cause rendering failure)
   - CORRECT: $f_i =$ or $f_i$ =
""".strip()

LATEX_BASIC_RULES = """
1. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
2. Use \\cdot for multiplication in math mode
3. Format large numbers with scientific notation where appropriate (e.g., $10^9$)
""".strip()

PROBLEM_STATEMENT_FORMAT_RULES = """
LQDOJ problem statement template (proposal guide section 5):
- Begin with the problem description; summarize the task when the story is long.
- Use level-four Input, Output and Example section headings (#### Input,
  #### Output, #### Example). Whitespace after #### is acceptable.
- Describe the input/output clearly; present constraints as readable bullet points.
- Give each example its own !!! question "Test X" block. Inside it use
  ???+ "Input" and ???+ success "Output", each containing a fenced sample
  code block (```sample). Indent each nested block by four additional spaces.
- Put an optional sample explanation in ??? warning "Note" inside that example.
  Do not require explanations when none are needed.
- Use #### Scoring for declared subtasks/point allocations when applicable.
  Do not invent subtasks or require a Scoring section for an unscored problem.
- Preserve the statement's language and mathematical meaning. Use inline LaTeX
  for mathematics, and follow the shared Markdown indentation/LaTeX rules.
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
    rules = rules.replace("\n4. Inline LaTeX", f"\n{start_number + 3}. Inline LaTeX")
    return rules


# Shared calibration for tagging and pre-publication review.
DIFFICULTY_RATING_RULES = "DIFFICULTY RATING GUIDELINES:\nUse the LQDOJ 100-3000 difficulty scale. Return an integer, preferably rounded to the nearest 100 unless the problem clearly sits between two buckets.\n\nRate the intended full-score solution, not only the implementation length or the names of standard algorithms used. If author's accepted source code is available, use it to infer the solution approach, but still judge the difficulty of discovering and proving that approach from the statement. If no source code is available, infer the intended full-score solution from the constraints, subtasks, scoring rules, and required output; do not default the rating downward merely because the implementation is unknown.\n\nProblem code, name, source, contest, division, and format may provide useful calibration context, especially when no accepted source code or editorial is available. Use that context as a weak prior after examining the actual task requirements; it should help resolve uncertainty, but it must not override clear evidence from the statement.\n\nBefore choosing points, privately evaluate these axes:\n- Key insight and modeling: how hard it is to find the main observation, reduction, invariant, construction, state representation, strategy, or objective transformation.\n- Algorithmic depth: whether the solution uses direct implementation, standard techniques, advanced combinations, unusual data structures, optimization, math, or ML methodology.\n- Correctness and edge cases: how much proof, case analysis, protocol/scoring reasoning, or constraint-sensitive reasoning is needed.\n- Implementation burden: code complexity, data handling, precision, optimization, debugging risk, and integration with the required submission format.\n\nBefore assigning points, privately form a plausible full-score solution outline: the main idea, why it is correct, and why it fits the constraints or scoring rules. If your outline is only a list of tags or a partial-subtask idea, treat the problem as harder and choose the higher plausible band.\n\nUse the hardest essential axis to calibrate the rating, not a simple average. A compact solution can still be very hard if the central model or proof is hidden. A long solution should be rated high only when the length reflects necessary algorithmic or reasoning difficulty rather than boilerplate. Familiar labels such as tree, greedy, DP, binary search, Fenwick tree, DSU, or segment tree do not cap the rating if the hard part is deriving the correct model, invariant, reduction, or proof.\n\nDo not reserve 2800-3000 only for unfamiliar technique names. Use 2800+ when the full solution requires discovering a structural theorem, hidden reduction, optimal strategy, compressed representation of a huge implicit state, or a construction whose validity is hard to prove for every input. Use 2500-2700 for one major non-obvious idea or several tightly interacting advanced ideas. Use 2100-2400 only when, after the main observation, the solution is mostly a known technique with moderate proof and implementation burden.\n\nFor constructive, output-object, signature-grader, or strategy tasks, include the difficulty of designing a valid object or sequence of actions, satisfying every checker condition, and optimizing it when required. If such a task asks for an optimal strategy or a globally valid construction under large constraints, rate the structural proof and correctness burden as a primary difficulty source, even if the final code uses familiar primitives.\n\nFor classical CP, calibrate as:\n- 100-300: First-programming exercises: print, read input, one arithmetic expression.\n- 400-600: Very easy conditionals/loops, direct formulas, simple digit/string processing.\n- 700-900: Easy implementation, simple math, basic simulation, straightforward ad-hoc.\n- 1000-1200: Non-trivial beginner/bronze problems requiring a small observation, simple greedy, prefix sums, or brute force within constraints.\n- 1300-1600: Standard algorithms: basic DP, graph traversal, binary search, sorting/two-pointers with a clear invariant.\n- 1700-2000: Advanced but still common techniques or combinations: harder DP, shortest paths, data structures, number theory, constructive reasoning.\n- 2100-2400: Hard problems requiring a substantial independent idea, optimization, proof, or combination of known techniques.\n- 2500-2700: Very hard problems requiring a non-obvious model, reduction, invariant, construction, strategy, or several advanced ideas that interact tightly.\n- 2800-3000: Elite problems where deriving the full solution is the main difficulty: deep or unusual modeling, difficult proof, communication/strategy constraints, output-only optimization, or multiple hard ideas that must all fit together.\n\nFor AI / output-only problems, calibrate as:\n- 100-600: Tutorial dataset task where a baseline script or direct rule is enough.\n- 700-1000: Clean tabular/image/text classification or regression with standard metric and obvious baseline.\n- 1100-1500: Bigger or noisier dataset, class imbalance, semi-supervised labels, feature engineering, or classical ML model selection expected.\n- 1600-2000: CV / NLP / signal task requiring non-trivial neural models, custom data loading, or careful preprocessing.\n- 2100-2400: Hard CV / NLP / structured prediction with strict constraints, retrieval / segmentation / multi-output scoring, or limited labels.\n- 2500-3000: Research-frontier AI tasks, novel modalities, LLM-judged outputs, dense prediction with custom metric, or difficult optimization."
