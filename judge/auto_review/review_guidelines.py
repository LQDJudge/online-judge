"""Technical review guidance, distinct from authoring UI instructions and examples."""

from llm_service.prompt_guidelines import STATEMENT_REVIEW_RULES

TECHNICAL_REVIEW_RULES = """
Assess supplied artifacts against the task contract and effective judging behavior.
Authoring documentation examples, UI steps and implementation tips are not mandatory
quality criteria. Accept equivalent correct implementations and configurations.
Report concrete consequences for validity, correctness, scoring, coverage or usable
limits. Do not flag coding style, filenames, a particular library or algorithm
merely because they differ from a template. Preserve preparation requirements;
passing preparation is evidence of readiness to review, not proof of correctness.
Cosmetic suggestions alone must not create warnings, NEEDS_AUTHOR_FIX or manual
review. Do not treat missing optional information or missing extra experiments as
an author defect. Material uncertainty must identify the evidence actually needed.
""".strip()

SECTION_GUIDANCE = {
    "statement_review": STATEMENT_REVIEW_RULES,
    "validator_review": """The validator reads each official input; exit zero means
valid. Assess parse failures, bounds, structural constraints and excess tokens.
Trailing whitespace is not the same as excess tokens; reject it only if the input
contract requires exact formatting. Testlib and hand-written validators are both
valid. A successful Run Validation establishes acceptance of official inputs,
not rejection of every invalid input. Do not invent challenge runs.""",
    "checker_review": """Assess the effective built-in or supplied custom checker,
including per-case overrides, tolerances and partial-score semantics. Built-ins
need no source upload; custom checker and Testlib protocols are distinct. For the
site's C++ custom checker protocol, argv contains input, contestant output and
answer paths; exit 0 accepts, 1 rejects, and 2 awards a ratio in [0,1] written to
stderr. Do not apply that protocol to Testlib/CMS or interactors. Use their actual
protocol/configuration. For metric tasks, assess direction, normalization and
schema handling; a complete valid reference need not achieve a perfect score.
Do not demand custom checking where a suitable built-in already handles the task.""",
    "test_review": """Both ZIP-backed and generator-backed packages are valid.
Generated input goes to stdout and expected output to stderr. A deterministic
construction needs no random seed; random generation should be reproducible from
its arguments/seed. Judge reproducibility rather than a specific RNG or formula.
Fast-I/O patterns are advice, not requirements without a demonstrated performance
issue. Assess actual boundary coverage, answer correctness and subtask aggregation;
there is no fixed minimum test count or required randomness. Optional statistics
are supplemental, not proof of semantics or grounds to reject a generator-only
package. A small adequate suite is preferable to a large redundant one.""",
    "solutions_review": """Assess sources independently, then compare expected
labels and actual executions. No fixed number of AC/WA/TLE solutions, particular
language portfolio, code naming or template style is mandatory. Assess whether
the evidence supports the actual task, with required full-test evidence enforced
by preparation. Missing additional languages or wrong-solution examples alone do
not prove a defect. A labelled AC is a claim, not proof; supplied incorrect
solutions are useful test-strength evidence rather than defects merely for being
incorrect. Account for declared subtasks instead of equating partial scoring
with an IOI format.""",
    "limits_review": """Use actual supplied results and sound complexity reasoning.
Do not invent mandatory headroom percentages, compiler speed ratios or execution
measurements. Missing reference solutions in additional allowed languages alone
are not a defect; flag a language limit only with a concrete supported concern.
Use the shared difficulty rubric to estimate difficulty independently.""",
}
