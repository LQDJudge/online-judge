from django.db import connection
from django.utils.translation import gettext_lazy

from judge.contest_format.ioi import IOIContestFormat
from judge.contest_format.registry import register_contest_format
from judge.models.problem_data import ProblemTestCase
from judge.timezone import from_database_time, to_database_time


@register_contest_format("ioi16")
class NewIOIContestFormat(IOIContestFormat):
    name = gettext_lazy("New IOI")
    config_defaults = {"cumtime": False}
    has_hidden_subtasks = True
    """
        cumtime: Specify True if time penalties are to be computed. Defaults to False.
    """

    def get_hidden_subtasks(self):
        queryset = self.contest.contest_problems.values_list("id", "hidden_subtasks")
        res = {}
        for problem_id, hidden_subtasks in queryset:
            subtasks = set()
            if hidden_subtasks:
                hidden_subtasks = hidden_subtasks.split(",")
                for i in hidden_subtasks:
                    try:
                        subtasks.add(int(i))
                    except Exception:
                        pass
            res[str(problem_id)] = subtasks
        return res

    def get_results_by_subtask(self, participation, include_frozen=False):
        frozen_time = self.contest.end_time
        if self.contest.freeze_after and not include_frozen:
            frozen_time = participation.start + self.contest.freeze_after

        # Match the bridge: batch numbers count S rows in order, not their IDs
        # or order values. Reordering cases after judging requires a rejudge.
        batch_modes = {}
        for problem_id, scoring in (
            ProblemTestCase.objects.filter(
                dataset_id__in=self.contest.contest_problems.values("problem_id"),
                type="S",
            )
            .order_by("dataset_id", "order")
            .values_list("dataset_id", "batch_scoring")
        ):
            batch_modes.setdefault(problem_id, []).append(scoring)

        best = {}
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cp.id,
                       cp.problem_id,
                       cp.points,
                       sub.date,
                       sub.id,
                       tc.batch,
                       SUM(tc.points),
                       SUM(tc.total),
                       MIN(CASE WHEN tc.total = 0 THEN 0.0
                                ELSE tc.points / tc.total END)
                FROM judge_contestproblem cp
                INNER JOIN judge_contestsubmission cs
                    ON cs.problem_id = cp.id AND cs.participation_id = %s
                INNER JOIN judge_submission sub
                    ON sub.id = cs.submission_id AND sub.status = 'D'
                INNER JOIN judge_submissiontestcase tc
                    ON tc.submission_id = sub.id
                WHERE sub.date < %s
                GROUP BY cp.id, cp.problem_id, cp.points, sub.date, sub.id, tc.batch
                """,
                (participation.id, to_database_time(frozen_time)),
            )

            for (
                contest_problem_id,
                problem_id,
                problem_points,
                date,
                submission_id,
                batch,
                points,
                total,
                min_fraction,
            ) in cursor:
                modes = batch_modes.get(problem_id, ())
                if (
                    batch is not None
                    and 1 <= batch <= len(modes)
                    and modes[batch - 1] == "min"
                    and total > 0
                ):
                    points = min_fraction * total

                # Select after scoring, keeping every field from the same best
                # submission. Equal scores use earliest date, then smallest ID.
                key = (contest_problem_id, batch)
                previous = best.get(key)
                if (
                    previous is None
                    or points > previous[3]
                    or (
                        points == previous[3]
                        and (date, submission_id) < (previous[2], previous[6])
                    )
                ):
                    best[key] = (
                        contest_problem_id,
                        problem_points,
                        date,
                        points,
                        total,
                        batch,
                        submission_id,
                    )

        return [
            best[key]
            for key in sorted(
                best, key=lambda key: (key[0], -1 if key[1] is None else key[1])
            )
        ]

    def update_participation(self, participation):
        hidden_subtasks = self.get_hidden_subtasks()

        def calculate_format_data(include_frozen):
            format_data = {}
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in self.get_results_by_subtask(participation, include_frozen):
                problem_id = str(problem_id)
                time = from_database_time(time)
                if self.config["cumtime"]:
                    dt = (time - participation.start).total_seconds()
                else:
                    dt = 0

                if format_data.get(problem_id) is None:
                    format_data[problem_id] = {
                        "points": 0,
                        "time": 0,
                        "total_points": 0,
                    }
                if (
                    subtask not in hidden_subtasks.get(problem_id, set())
                    or include_frozen
                ):
                    format_data[problem_id]["points"] += subtask_points
                format_data[problem_id]["total_points"] += total_subtask_points
                format_data[problem_id]["time"] = max(
                    dt, format_data[problem_id]["time"]
                )
                format_data[problem_id]["problem_points"] = problem_points

            return format_data

        def normalize_points(format_data):
            for problem_data in format_data.values():
                if not problem_data["total_points"]:
                    continue
                problem_data["points"] = (
                    problem_data["points"]
                    / problem_data["total_points"]
                    * problem_data["problem_points"]
                )

        # Public scores (excluding hidden subtasks)
        format_data = calculate_format_data(include_frozen=False)
        normalize_points(format_data)
        self.calculate_quiz_scores(participation, format_data)
        self.handle_frozen_state(participation, format_data)

        participation.score = round(
            self.compute_score(format_data),
            self.contest.points_precision,
        )
        participation.cumtime = self.compute_cumtime(format_data)
        participation.tiebreaker = 0
        participation.format_data = format_data

        # Final scores (including hidden subtasks)
        format_data_final = calculate_format_data(include_frozen=True)
        normalize_points(format_data_final)
        self.calculate_quiz_scores(participation, format_data_final)

        participation.score_final = round(
            self.compute_score(format_data_final),
            self.contest.points_precision,
        )
        participation.cumtime_final = self.compute_cumtime(format_data_final)
        participation.format_data_final = format_data_final

        self.apply_result_hidden(participation, format_data)
        participation.save()
