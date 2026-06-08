"""
Base class for contest-review checks.

Mirrors `judge/review/base.py`'s `ProblemReviewCheck` but bound to a Contest
instead of a Problem. We reuse the existing `CheckResultData` dataclass since
both review types translate to a single-letter status code; the runner per
review type maps that code to the appropriate concrete model row.

Contest checks may produce a WARNING status (in addition to SUCCESS / FAIL /
SKIPPED / ERROR) for advisory verdicts that should not block a publish but
still surface in the dashboard. Problem checks never use WARNING — its enum
was deliberately left untouched when contest checks were added.
"""

from judge.models import Contest
from judge.models.contest_review import ContestReviewRun
from judge.review.base import CheckResultData


class ContestReviewCheck:
    """
    Base class for a contest-review check.

    Each check inspects the contest (plus its problems / participants /
    submissions as needed) and returns a `CheckResultData`. Checks are
    self-sufficient: they skip (SKIPPED) when a required input is absent
    and do not depend on other checks' verdicts. Exceptions raised inside
    `run()` are caught by the runner and translated into an ERROR row.
    """

    id: str = ""  # stable identifier, e.g. "submission_leak_check"
    display_name: str = ""  # human-readable for the dashboard

    def run(self, contest: Contest, run: ContestReviewRun) -> CheckResultData:
        raise NotImplementedError
