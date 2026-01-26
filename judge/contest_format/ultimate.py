from django.utils.translation import gettext_lazy

from judge.contest_format.ioi import IOIContestFormat
from judge.contest_format.registry import register_contest_format
from django.db.models import OuterRef, Subquery

# This contest format only counts last submission for each problem.


@register_contest_format("ultimate")
class UltimateContestFormat(IOIContestFormat):
    name = gettext_lazy("Ultimate")

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
                id=Subquery(
                    queryset.filter(problem_id=OuterRef("problem_id"))
                    .order_by("-id")
                    .values("id")[:1]
                )
            )
            .values_list("problem_id", "submission__date", "points")
        )

        for problem_id, time, points in queryset:
            if self.config["cumtime"]:
                dt = (time - participation.start).total_seconds()
                if points:
                    cumtime += dt
            else:
                dt = 0
            format_data[str(problem_id)] = {
                "time": dt,
                "points": points,
            }
            score += points

        # Calculate quiz scores using base class method
        quiz_points = self.calculate_quiz_scores(participation, format_data)
        score += quiz_points

        self.handle_frozen_state(participation, format_data)
        participation.cumtime = max(cumtime, 0)
        participation.score = round(score, self.contest.points_precision)
        participation.tiebreaker = 0
        participation.format_data = format_data
        participation.save()
