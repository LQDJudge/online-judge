"""Small capability-specific instructions added to shared reviewer prompts."""

from judge.auto_review.problem_types import problem_profile_from_context


def profile_system_rules(context):
    profile = problem_profile_from_context(context)
    rules = []
    if profile.signature:
        rules.append(
            "Function-call task: use supplied headers/signatures/grader, enabled language families and templates. Solutions implement the required interface, not a stdin/stdout main program. Review ABI/API consistency from sources and ordinary judging results; do not require extra experiments."
        )
    if profile.execution == "interactive":
        rules.append(
            "Interactive task: review interactor source, protocol, flush/termination/query limits and Solution Code results. Static sources cannot establish every runtime interaction; identify concrete concerns without pretending extra runs happened."
        )
    if profile.execution == "communication":
        rules.append(
            "Communication task: review manager/process configuration, participant interfaces, per-process/aggregate resource semantics and ordinary results. Do not assume a batch stdout checker protocol."
        )
    if profile.submission == "output":
        rules.append(
            "Output-only task: assess submitted answer archives, required file formats, evaluator/data descriptions and actual reference scores, not missing source programs. Text input validation may not apply to binary datasets."
        )
    if profile.metric:
        rules.append(
            "Metric scoring: evaluate formula, direction, range, error handling and reference baseline quality. A valid complete baseline need not achieve a perfect score. Distinguish processing errors from low legitimate scores."
        )
    return "\n".join(rules)
