"""
Compute a deterministic content hash of a Contest for the dirty-check guard.

Covers every field the contest-review pipeline reads: contest metadata
(name/description/format/visibility/rating/window), the ordered problem set
(with each problem's own input_hash so any contained-problem change dirties
the contest hash), and the trusted-user set (because the leak check verdict
depends on who counts as trusted).

If a future contest check reads a new field, add it here AND add a test in
test_contest_review_hashing.py asserting the hash changes.
"""

import hashlib
import json

from judge.models import Contest
from judge.models.contest import ContestProblem
from judge.review.hashing import compute_input_hash


def compute_contest_input_hash(contest: Contest) -> str:
    payload = {
        "key": contest.key,
        "name": contest.name or "",
        "description": contest.description or "",
        "format_name": contest.format_name or "",
        "start_time": contest.start_time.isoformat() if contest.start_time else "",
        "end_time": contest.end_time.isoformat() if contest.end_time else "",
        "is_visible": bool(contest.is_visible),
        "is_rated": bool(contest.is_rated),
    }

    # Per-problem entry: order, declared points, and the problem's own input_hash.
    # Including the problem's hash means any contained-problem change (statement,
    # solution codes, test data) dirties the contest hash, which is what powers
    # the "skip if nothing changed since last review" optimisation in the runner.
    #
    # `problem__isnull=False`: ContestProblem.problem is nullable (quiz slots
    # have no problem). Null slots aren't reviewed — the problems_reviewed check
    # filters them out the same way — so they must be excluded here too, else
    # `cp.problem.code` below raises AttributeError and 500s the request.
    contest_problems = (
        ContestProblem.objects.filter(contest=contest, problem__isnull=False)
        .select_related("problem")
        .order_by("order", "id")
    )
    payload["problems"] = [
        {
            "code": cp.problem.code,
            "order": cp.order,
            "points": int(cp.points) if cp.points is not None else 0,
            "input_hash": compute_input_hash(cp.problem),
        }
        for cp in contest_problems
    ]

    # Quiz slots (ContestProblem.quiz set, problem null). Only the quiz's
    # identity/placement is hashed — NOT its content — so adding, removing,
    # reordering or repointing a quiz dirties the contest hash (letting the
    # author re-request review, e.g. to re-run the quiz-leak check), while a
    # quiz *content* edit does not. Content-level quiz review is a deferred
    # feature; when it lands, mix a quiz input_hash in here the same way
    # problems do above.
    quiz_slots = (
        ContestProblem.objects.filter(contest=contest, quiz__isnull=False)
        .select_related("quiz")
        .order_by("order", "id")
    )
    payload["quizzes"] = [
        {
            "code": cp.quiz.code,
            "order": cp.order,
            "points": int(cp.points) if cp.points is not None else 0,
        }
        for cp in quiz_slots
    ]

    # Trusted user set — drives the leak-check verdict. Sorted for determinism.
    trusted_ids = set()
    trusted_ids.update(contest.authors.values_list("id", flat=True))
    trusted_ids.update(contest.curators.values_list("id", flat=True))
    trusted_ids.update(contest.testers.values_list("id", flat=True))
    payload["trusted_user_ids"] = sorted(trusted_ids)

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
