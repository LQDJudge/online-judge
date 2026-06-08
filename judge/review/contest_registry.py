"""
Ordered list of contest-review checks executed by the runner.

Add new checks here in execution order. Cheapest first (DB-only) so a
fast-failing run surfaces its verdict quickly; LLM-heavy checks last so
their cost is only paid when the earlier checks haven't already failed
in a way that makes the LLM call moot. (The runner does not short-circuit —
every check always runs — but ordering still controls user-perceived latency
because the dashboard renders results as they land.)
"""

from judge.review.contest_checks.problems_reviewed import ProblemsReviewedCheck
from judge.review.contest_checks.submission_leak_check import SubmissionLeakCheck

CONTEST_CHECKS = [
    ProblemsReviewedCheck(),  # DB-only, depends on per-problem reviews
    SubmissionLeakCheck(),  # DB-only, aggregation query
]
