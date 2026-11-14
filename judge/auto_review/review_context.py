"""Build current review contexts and content identities."""

from judge.auto_review.contest_context import build_contest_context
from judge.auto_review.problem_context import (
    _batch_problem_identities,
    _capture_problem,
    hash_current_problem,
)
from judge.models import (
    Problem,
)


def run_context(run, *, for_display=False):
    """Current inputs for display, never a historical package representation."""
    attribute = "_review_display_context" if for_display else "_review_context"
    if not hasattr(run, attribute):
        if hasattr(run, "problem_id"):
            context = _capture_problem(
                run.problem, extract_pdf=not for_display, identity_only=for_display
            )
        else:
            children = list(
                run.child_dependencies.values(
                    "problem_id", "problem_run_id", "content_hash", "owned"
                )
            )
            context = build_contest_context(run.contest, children)
        setattr(run, attribute, context)
    return getattr(run, attribute)


def current_content_hash(obj, problem_hashes=None):
    if isinstance(obj, Problem):
        return hash_current_problem(obj)
    problems = list(
        obj.contest_problems.filter(problem__isnull=False)
        .order_by("order", "id")
        .values_list("problem_id", flat=True)
    )
    memo = problem_hashes if problem_hashes is not None else {}
    missing = [problem_id for problem_id in problems if problem_id not in memo]
    batch = _batch_problem_identities(missing) if missing else None
    children = []
    for problem_id in problems:
        if problem_id not in memo:
            memo[problem_id] = hash_current_problem(
                batch["problems"][problem_id], batch=batch
            )
        children.append({"problem_id": problem_id, "content_hash": memo[problem_id]})
    return build_contest_context(obj, children, identity_only=True)["input_hash"]
