from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.template.defaultfilters import floatformat
from django.utils.html import format_html
from django.utils.translation import gettext_lazy

from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format
from judge.timezone import from_database_time, to_database_time
from judge.utils.timedelta import nice_repr


@register_contest_format("atcoder")
class AtCoderContestFormat(DefaultContestFormat):
    name = gettext_lazy("AtCoder")
    config_defaults = {"penalty": 5}
    config_validators = {"penalty": lambda x: x >= 0}
    """
        penalty: Number of penalty minutes each incorrect submission adds. Defaults to 5.
    """

    @classmethod
    def validate(cls, config):
        if config is None:
            return

        if not isinstance(config, dict):
            raise ValidationError(
                "AtCoder-styled contest expects no config or dict as config"
            )

        for key, value in config.items():
            if key not in cls.config_defaults:
                raise ValidationError('unknown config key "%s"' % key)
            if not isinstance(value, type(cls.config_defaults[key])):
                raise ValidationError('invalid type for config key "%s"' % key)
            if not cls.config_validators[key](value):
                raise ValidationError(
                    'invalid value "%s" for config key "%s"' % (value, key)
                )

    def __init__(self, contest, config):
        self.config = self.config_defaults.copy()
        self.config.update(config or {})
        self.contest = contest

    def gather_results(self, participation):
        format_data = {}

        frozen_time = self.contest.end_time
        if self.contest.freeze_after:
            frozen_time = participation.start + self.contest.freeze_after

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT MAX(cs.points) as `score`, (
                    SELECT MIN(csub.date)
                        FROM judge_contestsubmission ccs LEFT OUTER JOIN
                             judge_submission csub ON (csub.id = ccs.submission_id)
                        WHERE ccs.problem_id = cp.id AND ccs.participation_id = %s AND ccs.points = MAX(cs.points)
                ) AS `time`, cp.id AS `prob`
                FROM judge_contestproblem cp INNER JOIN
                     judge_contestsubmission cs ON (cs.problem_id = cp.id AND cs.participation_id = %s) LEFT OUTER JOIN
                     judge_submission sub ON (sub.id = cs.submission_id)
                WHERE sub.date < %s
                GROUP BY cp.id
            """,
                (participation.id, participation.id, to_database_time(frozen_time)),
            )

            for score, time, prob in cursor.fetchall():
                time = from_database_time(time)
                dt = (time - participation.start).total_seconds()

                # Compute penalty
                if self.config["penalty"]:
                    # An IE can have a submission result of `None`
                    subs = (
                        participation.submissions.exclude(
                            submission__result__isnull=True
                        )
                        .exclude(submission__result__in=["IE", "CE"])
                        .filter(problem_id=prob)
                    )
                    if score:
                        prev = subs.filter(submission__date__lte=time).count() - 1
                    else:
                        # We should always display the penalty, even if the user has a score of 0
                        prev = subs.count()
                else:
                    prev = 0

                format_data[str(prob)] = {"time": dt, "points": score, "penalty": prev}

        return format_data

    def compute_cumtime(self, format_data, entries=None):
        cumtime = 0
        penalty = 0
        for key, entry in format_data.items():
            if entries is not None and key not in entries:
                continue
            if entry.get("points", 0) > 0:
                cumtime = max(cumtime, entry.get("time", 0))
                if self.config["penalty"] and entry.get("penalty"):
                    penalty += entry["penalty"] * self.config["penalty"] * 60
        return cumtime + penalty

    def display_user_problem(self, participation, contest_problem, show_final=False):
        if contest_problem.quiz_id:
            format_key = f"quiz_{contest_problem.id}"
        else:
            format_key = str(contest_problem.id)
        format_data = (participation.format_data or {}).get(format_key)
        if format_data:
            penalty = (
                format_html(
                    '<small style="color:red"> ({penalty})</small>',
                    penalty=floatformat(format_data["penalty"]),
                )
                if format_data.get("penalty")
                else ""
            )
            return self.display_problem_cell(
                participation,
                contest_problem,
                format_data,
                points=floatformat(format_data["points"]),
                extra=penalty,
                time=nice_repr(
                    timedelta(seconds=format_data["time"]), "noday-no-seconds"
                ),
                time_seconds=int(format_data["time"]),
            )
        else:
            return self.display_empty_cell(contest_problem)
