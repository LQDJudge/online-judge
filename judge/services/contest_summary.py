"""Public performance boards, independently refreshed from contest scoreboards."""

import math
from collections import defaultdict
from itertools import groupby

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


def validate_scores(scores):
    try:
        valid = (
            isinstance(scores, list)
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                for value in scores
            )
            and math.isfinite(sum(scores))
        )
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise ValidationError(_("Awards must be a list of finite numbers."))


def calculate_summary(summary):
    validate_scores(summary.scores)
    contests = list(summary.contests.order_by("start_time", "pk"))
    totals = defaultdict(float)
    cells = defaultdict(lambda: [[0, 0] for contest in contests])
    for column, contest in enumerate(contests):
        participants = (
            contest.users.filter(virtual=0, is_disqualified=False)
            .order_by(
                "-score",
                "cumtime",
                "tiebreaker",
                "pk",
            )
            .values("user_id", "score", "cumtime", "tiebreaker")
        )
        position = 1
        for _, tied in groupby(
            participants, key=lambda p: (p["score"], p["cumtime"], p["tiebreaker"])
        ):
            tied = list(tied)
            award = sum(summary.scores[position - 1 : position - 1 + len(tied)]) / len(
                tied
            )
            for participant in tied:
                user_id = participant["user_id"]
                totals[user_id] += award
                if not math.isfinite(totals[user_id]):
                    raise ValidationError(_("The total award is too large."))
                cells[user_id][column] = [award, position]
            position += len(tied)
    rows, rank, previous = [], 0, None
    for position, user_id in enumerate(
        sorted(totals, key=lambda pk: (-totals[pk], pk)), 1
    ):
        if totals[user_id] != previous:
            rank = position
        previous = totals[user_id]
        rows.append(
            [
                rank,
                {
                    "user_id": user_id,
                    "points": totals[user_id],
                    "point_contests": cells[user_id],
                },
            ]
        )
    return {"version": 1, "contest_ids": [c.pk for c in contests], "rows": rows}


def read_summary(results):
    """Legacy totals remain usable; never guess legacy positional column labels."""
    legacy = isinstance(results, list)
    if legacy:
        rows, contest_ids = results, []
    elif isinstance(results, dict) and results.get("version") == 1:
        rows, contest_ids = results.get("rows", []), results.get("contest_ids", [])
    else:
        return [], [], bool(results)
    try:
        if not isinstance(rows, list) or not isinstance(contest_ids, list):
            raise ValueError
        if any(type(pk) is not int for pk in contest_ids) or len(
            set(contest_ids)
        ) != len(contest_ids):
            raise ValueError
        for rank, item in rows:
            if type(rank) is not int or type(item["user_id"]) is not int:
                raise ValueError
            if not isinstance(item["points"], (float, int)) or not math.isfinite(
                item["points"]
            ):
                raise ValueError
            if not legacy:
                if len(item["point_contests"]) != len(contest_ids):
                    raise ValueError
                for award, position in item["point_contests"]:
                    if (
                        not isinstance(award, (float, int))
                        or not math.isfinite(award)
                        or type(position) is not int
                    ):
                        raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return [], [], True
    return rows, contest_ids, legacy
