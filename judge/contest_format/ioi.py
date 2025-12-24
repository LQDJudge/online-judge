from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Min, OuterRef, Subquery
from django.template.defaultfilters import floatformat
from django.utils.translation import gettext_lazy

from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format
from judge.utils.timedelta import nice_repr


@register_contest_format("ioi")
class IOIContestFormat(DefaultContestFormat):
    name = gettext_lazy("IOI")
    config_defaults = {"cumtime": False}
    """
        cumtime: Specify True if time penalties are to be computed. Defaults to False.
    """

    @classmethod
    def validate(cls, config):
        if config is None:
            return

        if not isinstance(config, dict):
            raise ValidationError(
                "IOI-styled contest expects no config or dict as config"
            )

        for key, value in config.items():
            if key not in cls.config_defaults:
                raise ValidationError('unknown config key "%s"' % key)
            if not isinstance(value, type(cls.config_defaults[key])):
                raise ValidationError('invalid type for config key "%s"' % key)

    def __init__(self, contest, config):
        self.config = self.config_defaults.copy()
        self.config.update(config or {})
        self.contest = contest

    def update_participation(self, participation):
        cumtime = 0
        score = 0
        format_data = {}

        queryset = participation.submissions
        if self.contest.freeze_after:
            queryset = queryset.filter(
                submission__date__lt=participation.start + self.contest.freeze_after
            )

        queryset = (
            queryset.values("problem_id")
            .filter(
                points=Subquery(
                    queryset.filter(problem_id=OuterRef("problem_id"))
                    .order_by("-points")
                    .values("points")[:1]
                )
            )
            .annotate(time=Min("submission__date"))
            .values_list("problem_id", "time", "points")
        )

        for problem_id, time, points in queryset:
            if self.config["cumtime"]:
                dt = (time - participation.start).total_seconds()
                if points:
                    cumtime += dt
            else:
                dt = 0

            format_data[str(problem_id)] = {"points": points, "time": dt}
            score += points

        self.handle_frozen_state(participation, format_data)
        participation.cumtime = max(cumtime, 0)
        participation.score = round(score, self.contest.points_precision)
        participation.tiebreaker = 0
        participation.format_data = format_data
        participation.save()

    def display_user_problem(self, participation, contest_problem, show_final=False):
        if show_final:
            format_data = (participation.format_data_final or {}).get(
                str(contest_problem.id)
            )
        else:
            format_data = (participation.format_data or {}).get(str(contest_problem.id))
        if format_data:
            time_seconds = int(format_data["time"]) if self.config["cumtime"] else None
            time_display = (
                nice_repr(timedelta(seconds=format_data["time"]), "noday-no-seconds")
                if self.config["cumtime"]
                else ""
            )
            return self.display_problem_cell(
                participation,
                contest_problem,
                format_data,
                points=floatformat(
                    format_data["points"], -self.contest.points_precision
                ),
                time=time_display,
                time_seconds=time_seconds,
            )
        else:
            return self.display_empty_cell(contest_problem)
