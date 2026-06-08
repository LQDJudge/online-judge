"""Shared verdict logic for review-status surfaces.

The admin internal queue (`judge/views/internal.py::InternalProblemQueue`),
the per-item dashboards (`judge/views/review.py`, `judge/views/contest_review.py`),
and the review list pages all need to answer the same question — "given an
item, what's the auto-review verdict pill to render?"

Pulled out as a separate module so any new surface gets the N+1-safe batched
query for free instead of copy-pasting the closure.

Verdict vocabulary (must stay in sync with template pills + scss colors):
  - 'pass'    DONE run, zero FAIL check results
  - 'fail'    DONE run, at least one FAIL check result
  - 'running' RUNNING (in flight, possibly waiting on Celery)
  - 'error'   ERROR (worker crashed / unrecoverable)
  - None      no run on record
"""


def verdict_for_run(run, run_class, fail_run_ids):
    """Map a single ReviewRun (+ the set of run-ids-with-any-FAIL) to a UI verdict.

    `fail_run_ids` should be precomputed once per request via the batched
    query in `batched_verdicts`. Doing it per-row triggers N+1.
    """
    if run is None:
        return None
    if run.status == run_class.RUNNING:
        return "running"
    if run.status == run_class.ERROR:
        return "error"
    return "fail" if run.id in fail_run_ids else "pass"


def batched_verdicts(item_ids, run_class, check_class, fk_name):
    """Compute `{item_id -> latest_run}` and `{item_id -> verdict}` in two queries.

    `fk_name` is the FK attribute on the run model that points at the item
    ('problem_id' for ProblemReviewRun, 'contest_id' for ContestReviewRun).

    Two queries total, regardless of how many items: one for the latest non-
    superseded run per item, one to check which DONE runs have any FAIL
    result. Mirrors the pattern in the admin queue but parametrized so both
    surfaces can use it.
    """
    if not item_ids:
        return {}, {}

    fk_filter = {f"{fk_name}__in": item_ids}
    # `.only()` keeps the SELECT skinny — `summary_report` can be large and
    # we never need it for the verdict pill. Secondary `-id` order makes the
    # winner deterministic when two runs share started_at (e.g. bulk import).
    latest = {}
    for r in (
        run_class.objects.filter(**fk_filter, superseded_by__isnull=True)
        .only("id", "status", fk_name, "started_at")
        .order_by("-started_at", "-id")
    ):
        # setdefault wins because we ordered by started_at DESC — first run we
        # see per item is the latest.
        latest.setdefault(getattr(r, fk_name), r)

    done_ids = [r.id for r in latest.values() if r.status == run_class.DONE]
    fail_set = set(
        check_class.objects.filter(run_id__in=done_ids, status=check_class.FAIL)
        .values_list("run_id", flat=True)
        .distinct()
    )
    verdicts = {
        item_id: verdict_for_run(run, run_class, fail_set)
        for item_id, run in latest.items()
    }
    return latest, verdicts
