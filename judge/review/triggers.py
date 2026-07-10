"""
Shared helpers to trigger an auto-review run for a Problem.

Two callers exist today:
  1. The author-facing "Request public" button (judge.views.internal.request_public)
     — creates a PublicRequest row AND dispatches `review_problem` via Celery.
  2. The contest auto-review pipeline (problems_reviewed check)
     — wants a fresh per-problem review but MUST NOT create a PublicRequest
     (otherwise contest-only problems would land in the admin's public queue
     and risk being accidentally published outside the contest's intended scope).

Both callers share the same atomic "create new run + supersede prior" step,
which is what this module wraps. The PublicRequest creation stays at the
caller site (only path 1 does it).

The helper supports two dispatch modes:
  - dispatch="celery": use review_problem.delay(...) on commit. The default
    for user-facing flows so the HTTP response returns immediately.
  - dispatch="sync": run review_problem.apply(...).get() inline. Used inside
    the contest review Celery task so the contest's verdict can reflect the
    fresh per-problem outcome.

Race-safety: caller is responsible for holding the SELECT…FOR UPDATE lock on
the Problem row before entering the helper, because the helper assumes nothing
else will create a competing ProblemReviewRun during its transaction.
"""

from django.db import transaction

from judge.models.problem_review import ProblemReviewRun
from judge.review.hashing import compute_input_hash


def trigger_problem_review_for(
    problem,
    profile,
    *,
    dispatch="celery",
    emit_notifications=True,
):
    """Create a fresh ProblemReviewRun and dispatch the review_problem task.

    Returns the created ProblemReviewRun (already saved). Supersedes any
    prior non-superseded run for the same problem so the new one becomes
    the "latest".

    Args:
        problem: judge.models.Problem
        profile: judge.models.Profile triggering the run
        dispatch: "celery" → fire-and-forget via review_problem.delay();
                  "sync"   → run review_problem.apply().get() inline (blocks).
        emit_notifications: forwarded to review_problem. Pass False from the
                  contest-review pipeline so the per-problem review doesn't
                  emit its own "new public request"/"review done" notifications
                  — the contest review emits contest-level notifications and
                  the per-problem ones would be duplicate spam (contest runs
                  create no PublicRequest, so the problem isn't in the queue
                  the notification links to). Leave True for author "Request
                  public" and admin Rerun flows.

    Caller is expected to be inside a transaction.atomic() block — this helper
    does NOT open its own to keep composition flexible (e.g., the contest path
    wraps multiple problem triggers in one atomic block).
    """
    # Local import: review_problem is a Celery task whose module imports
    # judge.review.* — importing it at module load creates a circular path.
    from judge.tasks.review import review_problem

    new_hash = compute_input_hash(problem)

    new_run = ProblemReviewRun.objects.create(
        problem=problem,
        triggered_by=profile,
        input_hash=new_hash,
    )

    # Supersede ALL prior non-superseded runs for this problem — not just the
    # latest. Defensive: if two runs ever ended up as parallel "heads" (e.g.,
    # via a race that escaped some prior locking bug), this collapses both
    # under the new one rather than leaving an inconsistent dashboard.
    ProblemReviewRun.objects.filter(
        problem=problem, superseded_by__isnull=True
    ).exclude(id=new_run.id).update(superseded_by=new_run)

    if dispatch == "celery":
        # on_commit so the worker doesn't pick up the row before our INSERT
        # is durably visible (avoids "ProblemReviewRun matching query does
        # not exist" inside the task).
        transaction.on_commit(
            lambda: review_problem.delay(
                new_run.id, emit_notifications=emit_notifications
            )
        )
    elif dispatch == "sync":
        # Sync path is called from within another Celery worker (the contest
        # review pipeline). `.apply()` runs the task body in the current
        # process and returns an EagerResult once the body returns — no
        # broker round-trip, no result-backend roundtrip.
        #
        # We DO NOT call .get() afterwards: Celery's safety check
        # `assert_will_not_block` refuses .get() calls inside a running task
        # body to prevent worker-pool deadlock. .apply() already blocked
        # until the task body returned, so .get() would only add a redundant
        # synchronous wait that triggers the guard.
        #
        # `throw=True` re-raises any exception the task raised, which is the
        # behavior the caller expects (the check's except-Exception block
        # catches and logs it).
        review_problem.apply(
            args=[new_run.id],
            kwargs={"emit_notifications": emit_notifications},
            throw=True,
        )
    else:
        raise ValueError(f"trigger_problem_review_for: unknown dispatch={dispatch!r}")

    return new_run
