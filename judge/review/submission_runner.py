"""
Run a candidate solution against current problem data and wait for the verdict.

Wraps the internal-submission primitive used by ProblemEquivalenceVerifier
(see judge/utils/problem_equivalence.py). The pattern is:

    Submission.objects.create(status="QU", ...)
    SubmissionSource.objects.create(submission=..., source=...)
    submission.judge(rejudge=False, judge_id=...)
    poll until submission.is_graded
"""

import logging
import time

from django.conf import settings

from judge.models import Submission, SubmissionSource

logger = logging.getLogger(__name__)


class JudgeTimeout(Exception):
    """Raised when a submission does not finish judging before the deadline."""


def _create_submission(problem, language, source, profile, judge_id=None):
    """Create + dispatch an internal submission, returning the Submission row.

    Mirrors ProblemEquivalenceVerifier._clone_submission + .judge():
      1. Create Submission row in 'QU' (queued) status.
      2. Attach SubmissionSource holding the source code.
      3. Call submission.judge(rejudge=False, judge_id=...) to push it onto
         the bridge. The submission is dispatched but not yet judged on return.
    """
    submission = Submission.objects.create(
        user=profile,
        problem=problem,
        language=language,
        status="QU",
        result=None,
        points=None,
        case_points=0,
        case_total=0,
        time=None,
        memory=None,
    )
    SubmissionSource.objects.create(submission=submission, source=source)
    submission.judge(rejudge=False, judge_id=judge_id)
    return submission


def _poll_submission(submission_id, timeout_seconds):
    """Wait until a submission's status moves to 'D' (done), or raise JudgeTimeout."""
    poll_interval = 1.0
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        sub = Submission.objects.get(id=submission_id)
        if sub.status == "D":
            return sub
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 5.0)
    raise JudgeTimeout(f"Submission {submission_id} not done within {timeout_seconds}s")


def judge_and_wait(problem, language, source, profile, timeout_seconds=None):
    """Submit `source` for `problem`, wait for the verdict, return the Submission.

    Args:
        problem: judge.models.Problem instance.
        language: judge.models.Language instance.
        source: source code string.
        profile: judge.models.Profile to attribute the submission to.
        timeout_seconds: max seconds to wait. Defaults to
            settings.AUTO_REVIEW_JUDGE_WAIT_TIMEOUT_SECONDS (or 600).

    Raises:
        JudgeTimeout: if the submission has not finished judging in time.
    """
    if timeout_seconds is None:
        timeout_seconds = getattr(
            settings, "AUTO_REVIEW_JUDGE_WAIT_TIMEOUT_SECONDS", 600
        )
    sub = _create_submission(problem, language, source, profile)
    if sub.status == "D":
        return sub
    return _poll_submission(sub.id, timeout_seconds)
