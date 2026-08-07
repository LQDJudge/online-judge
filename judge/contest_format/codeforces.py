from datetime import timedelta

from django.core.exceptions import ValidationError
from django.template.defaultfilters import floatformat
from django.utils.html import format_html
from django.utils.translation import gettext_lazy

from judge.contest_format.base import MAX_FORMAT_BONUS_POINTS
from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format
from judge.utils.timedelta import nice_repr


@register_contest_format("codeforces")
class CodeforcesContestFormat(DefaultContestFormat):
    name = gettext_lazy("Codeforces")
    config_defaults = {"penalty": 50, "cumtime": False}
    config_validators = {
        "penalty": lambda x: 0 <= x <= MAX_FORMAT_BONUS_POINTS,
        "cumtime": lambda x: isinstance(x, bool),
    }
    """
        penalty: Score deducted for each non-accepted submission before the first
                 fully accepted submission. Defaults to 50.
        cumtime: Whether to use cumulative time as a secondary tiebreaker and show
                 it in the total column. Defaults to False.
    """

    @classmethod
    def validate(cls, config):
        if config is None:
            return

        if not isinstance(config, dict):
            raise ValidationError(
                "Codeforces-styled contest expects no config or dict as config"
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
        partial_results = {}

        frozen_time = self.contest.end_time
        if self.contest.freeze_after:
            frozen_time = participation.start + self.contest.freeze_after
        duration = self.get_duration_seconds(participation)

        queryset = (
            participation.submissions.select_related("problem", "submission")
            .filter(submission__date__lt=frozen_time)
            .exclude(submission__result__isnull=True)
            .exclude(submission__result__in=["IE", "CE"])
            .order_by("problem_id", "submission__date", "submission_id")
        )

        for contest_submission in queryset:
            contest_problem = contest_submission.problem
            problem_id = str(contest_problem.id)
            score = contest_problem.points
            dt = (
                contest_submission.submission.date - participation.start
            ).total_seconds()

            entry = partial_results.setdefault(
                problem_id,
                {
                    "score": score,
                    "initial_ac_score": self.get_initial_ac_score(contest_problem),
                    "partial_points": 0,
                    "partial_time": dt,
                    "submission_index": 0,
                    "is_ac": False,
                },
            )

            if entry["is_ac"]:
                continue

            points = contest_submission.points or 0
            if points > entry["partial_points"]:
                entry["partial_points"] = points
                entry["partial_time"] = dt

            if score > 0 and points >= score:
                entry["is_ac"] = True
                entry["time"] = dt
                entry["points"] = self.compute_accepted_points(
                    score,
                    entry["initial_ac_score"],
                    dt,
                    duration,
                    entry["submission_index"],
                )
            else:
                entry["submission_index"] += 1

        for problem_id, entry in partial_results.items():
            if not entry["is_ac"]:
                entry["points"] = entry["partial_points"]
                entry["time"] = entry["partial_time"]

            format_data[problem_id] = {
                "time": entry["time"],
                "points": entry["points"],
                "score": entry["score"],
                "initial_ac_score": entry["initial_ac_score"],
                "partial": (
                    entry["partial_points"] / entry["score"] if entry["score"] else 0
                ),
                "submission_index": entry["submission_index"],
                "penalty": self.config["penalty"] * entry["submission_index"],
                "is_ac": entry["is_ac"],
            }

        return format_data

    def get_initial_ac_score(self, contest_problem):
        if contest_problem.initial_ac_score is not None:
            return max(contest_problem.initial_ac_score, contest_problem.points)
        return round((10 / 3) * contest_problem.points)

    def get_duration_seconds(self, participation):
        end_time = participation.end_time or self.contest.end_time
        return max((end_time - participation.start).total_seconds(), 0)

    def compute_accepted_points(
        self, score, initial_ac_score, seconds, duration, submission_index
    ):
        progress = min(max(seconds, 0), duration) / duration if duration else 1
        time_score = initial_ac_score - (initial_ac_score - score) * progress
        decayed_score = time_score - self.config["penalty"] * submission_index
        return max(score, decayed_score)

    def compute_cumtime(self, format_data, entries=None):
        if not self.config["cumtime"]:
            return 0
        return super().compute_cumtime(format_data, entries)

    def get_cell_state(self, contest_problem, format_data):
        if format_data.get("is_ac"):
            return "full-score" + (" frozen" if format_data.get("frozen") else "")
        return super().get_cell_state(contest_problem, format_data)

    def display_user_problem(self, participation, contest_problem, show_final=False):
        if contest_problem.quiz_id:
            format_key = f"quiz_{contest_problem.id}"
        else:
            format_key = str(contest_problem.id)
        format_data = (participation.format_data or {}).get(format_key)
        if format_data:
            penalty = (
                format_html(
                    '<small class="red"> -{penalty}</small>',
                    penalty=floatformat(
                        format_data["penalty"], -self.contest.points_precision
                    ),
                )
                if format_data.get("is_ac") and format_data.get("penalty")
                else ""
            )
            return self.display_problem_cell(
                participation,
                contest_problem,
                format_data,
                points=floatformat(
                    format_data["points"], -self.contest.points_precision
                ),
                extra=penalty,
                time=nice_repr(
                    timedelta(seconds=format_data["time"]), "noday-no-seconds"
                ),
                time_seconds=int(format_data["time"]),
            )
        else:
            return self.display_empty_cell(contest_problem)

    def get_contest_problem_label_script(self):
        return """
            function(n)
                n = n + 1
                ret = ""
                while n > 0 do
                    ret = string.char((n - 1) % 26 + 65) .. ret
                    n = math.floor((n - 1) / 26)
                end
                return ret
            end
        """
