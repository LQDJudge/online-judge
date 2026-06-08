"""time_limit_headroom: AC-expected ProblemSolutionCodes must use < TL_HEADROOM_RATIO of TL.

Reads ProblemSolutionCode entries instead of submission tags. A code counts
toward the headroom test when ALL of:
  - expected_result == 'AC' (author's declared intent — only main_ac-style
    solutions should be required to fit in the budget; brute_force entries
    are *expected* to TLE and shouldn't fail this check)
  - last_submission is not None and last_submission.status == 'D' (judging
    finished — we have real timing data)
  - last_submission.result == 'AC' (actually passed all tests)

Author-intent + actual-result both gating prevents two failure modes:
  - A brute_force solution (expected_result='TLE') accidentally counted
    against headroom because it just happened to finish fast on small tests.
  - A solution the author intended to AC but actually got TLE counted as
    "barely fits" when it's really broken.
"""

from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_data import ProblemSolutionCode
from judge.models.problem_review import ProblemReviewCheckResult
from judge.review.base import ProblemReviewCheck, CheckResultData


class TimeLimitHeadroomCheck(ProblemReviewCheck):
    id = "time_limit_headroom"
    display_name = gettext_lazy("Time limit")

    def run(self, problem, run):
        ratio_threshold = getattr(settings, "AUTO_REVIEW_TL_HEADROOM_RATIO", 0.8)
        tl = float(problem.time_limit) if problem.time_limit else 0.0
        if tl <= 0:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_("No time limit set."),
            )

        ac_codes = [
            sc
            for sc in ProblemSolutionCode.objects.filter(
                problem=problem
            ).select_related("last_submission")
            if sc.expected_result == "AC"
            and sc.last_submission is not None
            and sc.last_submission.status == "D"
            and sc.last_submission.result == "AC"
        ]

        if not ac_codes:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_(
                    "No AC-expected solution code with a successful run yet — "
                    "add one in Solution Codes (expected=AC) and click Run."
                ),
            )

        per_code = []
        violations = []
        for sc in ac_codes:
            sub = sc.last_submission
            t = float(sub.time) if sub.time else 0.0
            ratio = t / tl if tl else 0.0
            per_code.append(
                {
                    "solution_code_id": sc.id,
                    "name": sc.name or f"Code #{sc.order + 1}",
                    "submission_id": sub.id,
                    "time": t,
                    "time_limit": tl,
                    "ratio": ratio,
                }
            )
            if ratio >= ratio_threshold:
                violations.append(per_code[-1])

        if violations:
            # Worst offender — the slowest one — gives the most useful reason.
            worst = max(violations, key=lambda r: r["ratio"])
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "Solution code '%(name)s' uses %(pct).1f%% of the time limit — exceeds %(threshold)d%% threshold."
                )
                % {
                    "name": worst["name"],
                    "pct": worst["ratio"] * 100,
                    "threshold": int(ratio_threshold * 100),
                },
                details={"codes": per_code, "violations": violations},
            )

        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("All AC solution codes use < %(threshold)d%% of the time limit.")
            % {
                "threshold": int(ratio_threshold * 100),
            },
            details={"codes": per_code, "violations": []},
        )
