"""Trusted specialist extensions for composable judging capabilities."""

from judge.review.types import snapshot_problem_profile


def profile_system_rules(snapshot):
    profile = snapshot_problem_profile(snapshot)
    rules = []
    if profile.signature:
        rules.append(
            """FUNCTION/SIGNATURE PROFILE: The contestant implements the public API;
absence of main is expected. Check public signatures, index/types, repeated calls,
state reset and each enabled language family's private grader against the statement.
Public headers/templates are contestant information; private grader source is not
part of the blind solve. H5_PLAN must include executable small legal and wrong
implementations using candidate_source and an enabled language runtime key on each
probe. Cover every enabled interface family unless its prepared full-AC evidence
already establishes that family. Inputs and reference outputs need independent
justification. Graders may rely on valid official inputs; do not demand duplicate
input validation inside a function harness. Missing compiler/runtime or a faulty generated helper is not an
author defect. Observed small-case agreement cannot prove full correctness.
R may use its one follow-up round to repair a challenger: request profile_probe
with payload.source={source,language} and payload.cases. The system supplies the
frozen official files; do not propose replacing them."""
        )
    if profile.execution in ("interactive", "communication"):
        rules.append(
            """INTERACTION PROFILE: E formalizes roles, initial knowledge, message grammar,
turn/flush/termination rules, legal channels, query/message/byte budgets and scoring.
FG independently derives a strategy, proof and adversarial states without private
manager/interactor source. H2 analyzes complete contestant behavior, H3 checks hidden
states and interaction paths, H4 validates underlying inputs, H5 checks the actual
interactor/manager and optional final checker together. H5_PLAN proposes small
executable legal and deliberately wrong/protocol-violating contestants in probes:
candidate_source is the full contestant source; language is an enabled runtime key;
input is the hidden state; expected_output is the applicable reference or empty;
candidate_output is empty for executable challenges. Explain expected behavior.
Do not invent a static contestant output as a substitute for an interaction.
Include meaningful acceptance and rejection cases. Premature EOF, deadlock, timeout,
manager crash or helper failure is not proof of correct rejection. Interpret actual
observations and their limits; no invented transcripts. Review adaptive consistency
when applicable. Hidden-state probes must satisfy the official input contract;
do not require the interactor/manager to duplicate a separate input validator.
For communication, check all roles and intended information
restrictions, early manager decisions, process participation and aggregate contestant
CPU/memory accounting separately from manager allowances. Separate processes alone
do not establish all intended information restrictions. H7 compares substantive
task/protocol differences, not just an interactive wrapper."""
        )
    if profile.submission == "output" or profile.metric:
        rules.append(
            """OUTPUT/SCORING PROFILE: Assess actual submitted artifact bytes and observed
scores, not fictitious executable source. Valid partial scores are not inherently
defects and no perfect-score submission or universal training code is required.
Identical artifact hashes are repeated evidence, not independent coverage.
Distinguish raw metric, normalized fraction and awarded points. Independently verify
small scorer examples, direction, normalization, alignment, domain errors, nonfinite
values and configured public/full scoring. Use expected_score_min/max for justified
normalized score intervals with expected_claim='score'; propose at least two
independently calculated, different scores. For configured pretest fractions also
supply expected_pretest_score_min/max for the same fixtures: the system executes
both modes without changing live contest settings. Binary scorer fixtures may use
expected_output_b64/candidate_output_b64; keep the required text fields empty and
never pretend UTF-8 text is the binary format. Check feasibility separately from optimization quality.
H3/H4 inspect bounded dataset/schema evidence and disclose sampling/unread portions.
Do not infer training time from upload/scoring runtime or scientific validity from
a finite review. Hidden labels/private reference files stay out of blind solving.
Account exposure remains outside technical quality review."""
        )
    return "\n\n".join(rules)
