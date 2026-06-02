"""time_limit_headroom: main AC submissions must use < TL_HEADROOM_RATIO of TL."""

from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewSubmissionTag,
)
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

        all_tags = ProblemReviewSubmissionTag.objects.filter(
            submission__problem=problem
        ).select_related("submission")

        # Treat any fully-AC tagged submission as a main-AC candidate. This avoids
        # requiring authors to label `kind=MAIN`; the LLM-driven rubric handles roles.
        ac_tags = [
            tag
            for tag in all_tags
            if tag.submission.case_total
            and tag.submission.case_points == tag.submission.case_total
        ]

        if not ac_tags:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_(
                    "No fully-AC reference submission — depends on solutions_rubric reference set."
                ),
            )

        per_submission = []
        violations = []
        for tag in ac_tags:
            sub = tag.submission
            t = float(sub.time) if sub.time else 0.0
            ratio = t / tl if tl else 0.0
            per_submission.append(
                {
                    "submission_id": sub.id,
                    "max_test_time": t,
                    "time_limit": tl,
                    "ratio": ratio,
                }
            )
            if ratio >= ratio_threshold:
                violations.append(
                    {
                        "submission_id": sub.id,
                        "ratio": ratio,
                    }
                )

        if violations:
            v = violations[0]
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "Main AC submission %(sid)d uses %(pct).1f%% of the time limit — exceeds %(threshold)d%% threshold."
                )
                % {
                    "sid": v["submission_id"],
                    "pct": v["ratio"] * 100,
                    "threshold": int(ratio_threshold * 100),
                },
                details={"submissions": per_submission, "violations": violations},
            )

        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("All main AC submissions use < %(threshold)d%% of the time limit.")
            % {
                "threshold": int(ratio_threshold * 100),
            },
            details={"submissions": per_submission, "violations": []},
        )
