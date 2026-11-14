"""Shared saved-assessment labels for review pages, dashboards and queues."""


def verdict_for_run(run):
    """Completion alone is never a passing assessment."""
    if run is None:
        return None
    if run.current_stale:
        return "stale"
    if run.status == "R":
        return "running"
    if run.status == "C":
        return "cancelled"
    if run.status in ("B", "E"):
        return "error"
    if run.status != "D":
        return "incomplete"
    readiness = run.readiness
    return {
        "READY": "pass",
        "READY_WITH_WARNINGS": "warning",
        "NEEDS_AUTHOR_FIX": "fail",
        "MANUAL_REVIEW": "manual",
        "INCOMPLETE": "incomplete",
    }.get(readiness, "incomplete")


def batched_verdicts(item_ids, run_class, fk_name):
    """Load the latest saved verdicts in one query, independent of row count."""
    if not item_ids:
        return {}, {}
    if fk_name not in ("problem_id", "contest_id"):
        raise ValueError("Unknown review object relation")
    latest = {}
    runs = (
        run_class.objects.filter(
            **{fk_name + "__in": item_ids}, superseded_by__isnull=True
        )
        .only(
            "id",
            fk_name,
            "status",
            "readiness",
            "current_stale",
            "started_at",
            "finished_at",
        )
        .order_by("-started_at", "-id")
    )
    for run in runs:
        latest.setdefault(getattr(run, fk_name), run)
    return latest, {identity: verdict_for_run(run) for identity, run in latest.items()}
