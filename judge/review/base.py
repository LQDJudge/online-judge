"""
Base types and helpers for the auto-review pipeline.

See docs/superpowers/specs/2026-05-24-auto-review-pipeline-design.md
"""

from dataclasses import dataclass, field

from judge.models import Problem
from judge.models.problem_review import ProblemReviewRun


@dataclass
class CheckResultData:
    """Return value from ProblemReviewCheck.run(). The runner translates this into a ProblemReviewCheckResult row."""

    status: str  # one of ProblemReviewCheckResult.SUCCESS/FAIL/SKIPPED
    reason: str = ""
    details: dict = field(default_factory=dict)


class ProblemReviewCheck:
    """
    Base class for a problem-review check.

    Each check inspects the problem state and returns a CheckResultData.
    Checks are self-sufficient: they examine their own inputs and skip
    (SKIPPED) if a required input is absent. They do not depend on other
    checks' verdicts. Exceptions raised inside `run()` are caught by the
    runner and translated into an ERROR result.
    """

    id: str = ""  # stable identifier, e.g. "artifacts_present"
    display_name: str = ""  # human-readable for the dashboard

    def run(self, problem: Problem, run: ProblemReviewRun) -> CheckResultData:
        raise NotImplementedError
