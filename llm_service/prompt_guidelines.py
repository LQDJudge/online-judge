"""
Shared prompt guidelines for LLM services.
Contains common formatting rules for markdown, LaTeX, etc.
"""

MARKDOWN_FORMATTING_RULES = r"""
MARKDOWN RENDERING GUIDANCE:
1. Preserve intended nesting. In this renderer nested lists need four additional
   spaces; indent fenced blocks and admonition contents within their parent.
2. Display LaTeX may use $$...$$ on its own line, multiline $$ delimiters, or a
   fenced math block. The renderer pads standalone display blocks automatically:
   adjacent display blocks do not require blank lines or an aligned environment.
   Inside lists a correctly indented math fence can help retain nesting.
3. Prefer code spans for literal input/output strings. Valid \text{...} and
   \texttt{...} inside mathematics are also allowed. Preserve clear equivalent
   notation; neither \cdot nor scientific notation is compulsory.
4. Use balanced math delimiters and code fences. Inline $...$ must not have
   whitespace immediately inside the delimiters because it prevents math parsing.
   Preserve code/sample content exactly and keep the matching fence indentation.
""".strip()

LATEX_BASIC_RULES = r"""
Use supported LaTeX for mathematics, with $...$ for inline expressions.
Preserve mathematical meaning and clear equivalent notation; multiplication signs
and scientific notation are presentation choices rather than requirements.
""".strip()

PROBLEM_STATEMENT_FORMAT_RULES = """
Preferred LQDOJ problem statement template for formatting tools:
- Begin with the task description. Prefer #### Input, #### Output and #### Example
  headings (or equivalent headings in the original language).
- Explain input/output and constraints clearly; bullet lists are useful but optional.
- Prefer one !!! question "Test X" block per example, containing ???+ "Input"
  and ???+ success "Output", each with a fenced sample code block (```sample).
  Indent each nested block by four additional spaces.
- Keep supplied explanations, optionally in ??? warning "Note". Do not invent one.
- Include scoring/subtasks only when supplied. Never invent point allocations.
- Preserve the original language, mathematical meaning and exact sample data.
These are formatting preferences, not review acceptance requirements. Equivalent
readable headings, paragraphs, tables and labelled sample blocks are valid.
""".strip()

STATEMENT_REVIEW_RULES = """
Assess task correctness, completeness, clarity and concrete rendering defects.
Require a clear task/interface, relevant constraints, input/output or submission
format and scoring where applicable; adapt to function, interactive and output-only
problems instead of demanding classical batch sections for every problem type.
Accept equivalent readable headings (including translated headings), paragraphs,
tables and labelled sample code blocks. Do not require exact heading levels,
constraint bullets, example admonitions, scientific notation or a multiplication
symbol. Adjacent display-math blocks are supported without blank lines; aligned
is optional. Use the shared rendering guidance to identify actual broken formulas,
fences or nesting, not cosmetic departures from a preferred template.
Missing optional editorial or unnecessary sample explanations are not defects.
Preserve sample data and distinguish objective contradictions from stylistic advice.
""".strip()


def get_markdown_rules_for_prompt(start_number: int = 1) -> str:
    """Return shared renderer guidance with caller-specific rule numbering."""
    rules = MARKDOWN_FORMATTING_RULES
    for number in range(4, 0, -1):
        rules = rules.replace(f"\n{number}.", f"\n{start_number + number - 1}.")
    return rules


# Shared calibration for tagging and pre-publication review.
DIFFICULTY_RATING_RULES = "DIFFICULTY RATING GUIDELINES:\nUse the LQDOJ 100-3000 difficulty scale. Return an integer, preferably rounded to the nearest 100 unless the problem clearly sits between two buckets.\n\nRate the intended full-score solution, not only the implementation length or the names of standard algorithms used. If author's accepted source code is available, use it to infer the solution approach, but still judge the difficulty of discovering and proving that approach from the statement. If no source code is available, infer the intended full-score solution from the constraints, subtasks, scoring rules, and required output; do not default the rating downward merely because the implementation is unknown.\n\nProblem code, name, source, contest, division, and format may provide useful calibration context, especially when no accepted source code or editorial is available. Use that context as a weak prior after examining the actual task requirements; it should help resolve uncertainty, but it must not override clear evidence from the statement.\n\nBefore choosing points, privately evaluate these axes:\n- Key insight and modeling: how hard it is to find the main observation, reduction, invariant, construction, state representation, strategy, or objective transformation.\n- Algorithmic depth: whether the solution uses direct implementation, standard techniques, advanced combinations, unusual data structures, optimization, math, or ML methodology.\n- Correctness and edge cases: how much proof, case analysis, protocol/scoring reasoning, or constraint-sensitive reasoning is needed.\n- Implementation burden: code complexity, data handling, precision, optimization, debugging risk, and integration with the required submission format.\n\nBefore assigning points, privately form a plausible full-score solution outline: the main idea, why it is correct, and why it fits the constraints or scoring rules. If your outline is only a list of tags or a partial-subtask idea, treat the problem as harder and choose the higher plausible band.\n\nUse the hardest essential axis to calibrate the rating, not a simple average. A compact solution can still be very hard if the central model or proof is hidden. A long solution should be rated high only when the length reflects necessary algorithmic or reasoning difficulty rather than boilerplate. Familiar labels such as tree, greedy, DP, binary search, Fenwick tree, DSU, or segment tree do not cap the rating if the hard part is deriving the correct model, invariant, reduction, or proof.\n\nDo not reserve 2800-3000 only for unfamiliar technique names. Use 2800+ when the full solution requires discovering a structural theorem, hidden reduction, optimal strategy, compressed representation of a huge implicit state, or a construction whose validity is hard to prove for every input. Use 2500-2700 for one major non-obvious idea or several tightly interacting advanced ideas. Use 2100-2400 only when, after the main observation, the solution is mostly a known technique with moderate proof and implementation burden.\n\nFor constructive, output-object, signature-grader, or strategy tasks, include the difficulty of designing a valid object or sequence of actions, satisfying every checker condition, and optimizing it when required. If such a task asks for an optimal strategy or a globally valid construction under large constraints, rate the structural proof and correctness burden as a primary difficulty source, even if the final code uses familiar primitives.\n\nFor classical CP, calibrate as:\n- 100-300: First-programming exercises: print, read input, one arithmetic expression.\n- 400-600: Very easy conditionals/loops, direct formulas, simple digit/string processing.\n- 700-900: Easy implementation, simple math, basic simulation, straightforward ad-hoc.\n- 1000-1200: Non-trivial beginner/bronze problems requiring a small observation, simple greedy, prefix sums, or brute force within constraints.\n- 1300-1600: Standard algorithms: basic DP, graph traversal, binary search, sorting/two-pointers with a clear invariant.\n- 1700-2000: Advanced but still common techniques or combinations: harder DP, shortest paths, data structures, number theory, constructive reasoning.\n- 2100-2400: Hard problems requiring a substantial independent idea, optimization, proof, or combination of known techniques.\n- 2500-2700: Very hard problems requiring a non-obvious model, reduction, invariant, construction, strategy, or several advanced ideas that interact tightly.\n- 2800-3000: Elite problems where deriving the full solution is the main difficulty: deep or unusual modeling, difficult proof, communication/strategy constraints, output-only optimization, or multiple hard ideas that must all fit together.\n\nFor AI / output-only problems, calibrate as:\n- 100-600: Tutorial dataset task where a baseline script or direct rule is enough.\n- 700-1000: Clean tabular/image/text classification or regression with standard metric and obvious baseline.\n- 1100-1500: Bigger or noisier dataset, class imbalance, semi-supervised labels, feature engineering, or classical ML model selection expected.\n- 1600-2000: CV / NLP / signal task requiring non-trivial neural models, custom data loading, or careful preprocessing.\n- 2100-2400: Hard CV / NLP / structured prediction with strict constraints, retrieval / segmentation / multi-output scoring, or limited labels.\n- 2500-3000: Research-frontier AI tasks, novel modalities, LLM-judged outputs, dense prediction with custom metric, or difficult optimization."
