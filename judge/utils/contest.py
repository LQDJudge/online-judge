from django.db import transaction
from judge.tasks import rescore_contest
from judge.models import (
    Contest,
)


def maybe_trigger_contest_rescore(form, contest, force_rescore=False):
    if (
        any(
            f in form.changed_data
            for f in (
                "start_time",
                "end_time",
                "time_limit",
                "format_config",
                "format_name",
                "freeze_after",
            )
        )
        or force_rescore
    ):
        transaction.on_commit(rescore_contest.s(contest.key).delay)

    if any(
        f in form.changed_data
        for f in (
            "authors",
            "curators",
            "testers",
        )
    ):
        Contest._author_ids.dirty(contest)
        Contest._curator_ids.dirty(contest)
        Contest._tester_ids.dirty(contest)
