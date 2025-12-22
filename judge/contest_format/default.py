from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from judge.contest_format.base import BaseContestFormat
from judge.contest_format.registry import register_contest_format
from judge.utils.timedelta import nice_repr


@register_contest_format("default")
class DefaultContestFormat(BaseContestFormat):
    name = gettext_lazy("Default")

    @classmethod
    def validate(cls, config):
        if config is not None and (not isinstance(config, dict) or config):
            raise ValidationError(
                "default contest expects no config or empty dict as config"
            )

    def __init__(self, contest, config):
        super(DefaultContestFormat, self).__init__(contest, config)

    def update_participation(self, participation):
        cumtime = 0
        points = 0
        format_data = {}

        queryset = participation.submissions

        if self.contest.freeze_after:
            queryset = queryset.filter(
                submission__date__lt=participation.start + self.contest.freeze_after
            )

        queryset = queryset.values("problem_id").annotate(
            time=Max("submission__date"),
            points=Max("points"),
        )

        for result in queryset:
            dt = (result["time"] - participation.start).total_seconds()
            if result["points"]:
                cumtime += dt
            format_data[str(result["problem_id"])] = {
                "time": dt,
                "points": result["points"],
            }
            points += result["points"]

        self.handle_frozen_state(participation, format_data)
        participation.cumtime = max(cumtime, 0)
        participation.score = round(points, self.contest.points_precision)
        participation.tiebreaker = 0
        participation.format_data = format_data
        participation.save()

    def display_empty_cell(self, contest_problem):
        """
        Returns the HTML fragment for an empty problem cell (no submissions).
        """
        return format_html(
            '<td class="problem-score-col" title="{tooltip}"></td>',
            tooltip=self.get_problem_tooltip(contest_problem),
        )

    def get_cell_state(self, contest_problem, format_data):
        """
        Returns the CSS state classes for a problem cell.
        """
        return (
            (
                "pretest-"
                if self.contest.run_pretests_only and contest_problem.is_pretested
                else ""
            )
            + self.best_solution_state(format_data["points"], contest_problem.points)
            + (" frozen" if format_data.get("frozen") else "")
        )

    def get_submission_url(self, participation, contest_problem):
        """
        Returns the URL for viewing user submissions for a problem.
        """
        return reverse(
            "contest_user_submissions_ajax",
            args=[self.contest.key, participation.id, contest_problem.problem.code],
        )

    def display_problem_cell(
        self,
        participation,
        contest_problem,
        format_data,
        points,
        extra="",
        time="",
        time_seconds=None,
    ):
        """
        Returns the HTML fragment for a problem cell with submission data.

        :param participation: The ContestParticipation object.
        :param contest_problem: The ContestProblem object.
        :param format_data: The format data dict for this problem.
        :param points: Formatted points string to display.
        :param extra: Optional extra HTML (e.g., penalty, bonus).
        :param time: Formatted time string to display.
        :param time_seconds: Time in seconds for data-time attribute (None to omit).
        """
        time_attr = (
            mark_safe(' data-time="{}"'.format(time_seconds))
            if time_seconds is not None
            else ""
        )
        return format_html(
            '<td class="{state} problem-score-col" title="{tooltip}"><a data-featherlight="{url}" href="#">{points}{extra}<div class="solving-time"{time_attr}>{time}</div></a></td>',
            state=self.get_cell_state(contest_problem, format_data),
            tooltip=self.get_problem_tooltip(contest_problem),
            url=self.get_submission_url(participation, contest_problem),
            points=points,
            extra=extra,
            time=time,
            time_attr=time_attr,
        )

    def display_user_problem(self, participation, contest_problem, show_final=False):
        format_data = (participation.format_data or {}).get(str(contest_problem.id))
        if format_data:
            return self.display_problem_cell(
                participation,
                contest_problem,
                format_data,
                points=floatformat(
                    format_data["points"], -self.contest.points_precision
                ),
                time=nice_repr(
                    timedelta(seconds=format_data["time"]), "noday-no-seconds"
                ),
                time_seconds=int(format_data["time"]),
            )
        else:
            return self.display_empty_cell(contest_problem)

    def display_participation_result(self, participation, show_final=False):
        return format_html(
            '<td class="user-points">{points}<div class="solving-time">{cumtime}</div></td>',
            points=floatformat(participation.score, -self.contest.points_precision),
            cumtime=nice_repr(
                timedelta(seconds=participation.cumtime), "noday-no-seconds"
            ),
        )

    def get_problem_breakdown(self, participation, contest_problems):
        return [
            (participation.format_data or {}).get(str(contest_problem.id))
            for contest_problem in contest_problems
        ]

    def get_contest_problem_label_script(self):
        return """
            function(n)
                return tostring(math.floor(n + 1))
            end
        """
