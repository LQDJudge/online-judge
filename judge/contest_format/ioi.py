from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format
from judge.timezone import from_database_time
from judge.utils.timedelta import nice_repr
from django.db.models import Min, OuterRef, Subquery


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
            return format_html(
                '<td class="{state} problem-score-col"><a data-featherlight="{url}" href="#">{points}<div class="solving-time">{time}</div></a></td>',
                state=(
                    (
                        "pretest-"
                        if self.contest.run_pretests_only
                        and contest_problem.is_pretested
                        else ""
                    )
                    + self.best_solution_state(
                        format_data["points"], contest_problem.points
                    )
                    + (" frozen" if format_data.get("frozen") else "")
                ),
                url=reverse(
                    "contest_user_submissions_ajax",
                    args=[
                        self.contest.key,
                        participation.id,
                        contest_problem.problem.code,
                    ],
                ),
                points=floatformat(
                    format_data["points"], -self.contest.points_precision
                ),
                time=(
                    nice_repr(timedelta(seconds=format_data["time"]), "noday")
                    if self.config["cumtime"]
                    else ""
                ),
            )
        else:
            return mark_safe('<td class="problem-score-col"></td>')

    def display_participation_result(self, participation, show_final=False):
        if show_final:
            score = participation.score_final
            cumtime = participation.cumtime_final
        else:
            score = participation.score
            cumtime = participation.cumtime
        return format_html(
            '<td class="user-points">{points}<div class="solving-time">{cumtime}</div></td>',
            points=floatformat(score, -self.contest.points_precision),
            cumtime=(
                nice_repr(timedelta(seconds=cumtime), "noday")
                if self.config["cumtime"]
                else ""
            ),
        )
