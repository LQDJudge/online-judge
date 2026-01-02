from django.db import connection
from django.utils.translation import gettext as _, gettext_lazy

from judge.contest_format.ioi import IOIContestFormat
from judge.contest_format.registry import register_contest_format
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
                    except Exception as e:
                        pass
            res[str(problem_id)] = subtasks
        return res

    def get_results_by_subtask(self, participation, include_frozen=False):
        frozen_time = self.contest.end_time
        if self.contest.freeze_after and not include_frozen:
            frozen_time = participation.start + self.contest.freeze_after

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT q.prob,
                       q.prob_points,
                       MIN(q.date) as `date`,
                       q.batch_points,
                       q.total_batch_points,
                       q.batch,
                       q.subid
                FROM (
                         SELECT cp.id          as `prob`,
                                cp.points      as `prob_points`,
                                sub.id         as `subid`,
                                sub.date       as `date`,
                                tc.points      as `points`,
                                tc.batch       as `batch`,
                                SUM(tc.points) as `batch_points`,
                                SUM(tc.total)  as `total_batch_points`
                         FROM judge_contestproblem cp
                                  INNER JOIN
                              judge_contestsubmission cs
                              ON (cs.problem_id = cp.id AND cs.participation_id = %s)
                                  LEFT OUTER JOIN
                              judge_submission sub
                              ON (sub.id = cs.submission_id AND sub.status = 'D')
                                  INNER JOIN judge_submissiontestcase tc
                              ON sub.id = tc.submission_id
                         WHERE sub.date < %s
                         GROUP BY cp.id, tc.batch, sub.id
                     ) q
                         INNER JOIN (
                    SELECT prob, batch, MAX(r.batch_points) as max_batch_points
                    FROM (
                             SELECT cp.id          as `prob`,
                                    tc.batch       as `batch`,
                                    SUM(tc.points) as `batch_points`
                             FROM judge_contestproblem cp
                                      INNER JOIN
                                  judge_contestsubmission cs
                                  ON (cs.problem_id = cp.id AND cs.participation_id = %s)
                                      LEFT OUTER JOIN
                                  judge_submission sub
                                  ON (sub.id = cs.submission_id AND sub.status = 'D')
                                      INNER JOIN judge_submissiontestcase tc
                                  ON sub.id = tc.submission_id
                             WHERE sub.date < %s
                             GROUP BY cp.id, tc.batch, sub.id
                         ) r
                    GROUP BY prob, batch
                ) p
                ON p.prob = q.prob AND (p.batch = q.batch OR p.batch is NULL AND q.batch is NULL)
                WHERE p.max_batch_points = q.batch_points
                GROUP BY q.prob, q.batch
            """,
                (
                    participation.id,
                    to_database_time(frozen_time),
                    participation.id,
                    to_database_time(frozen_time),
                ),
            )

            return cursor.fetchall()

    def update_participation(self, participation):
        hidden_subtasks = self.get_hidden_subtasks()

        def calculate_format_data(participation, include_frozen):
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

        def recalculate_results(format_data):
            cumtime = 0
            score = 0
            for problem_data in format_data.values():
                if not problem_data["total_points"]:
                    continue
                penalty = problem_data["time"]
                problem_data["points"] = (
                    problem_data["points"]
                    / problem_data["total_points"]
                    * problem_data["problem_points"]
                )
                if self.config["cumtime"] and problem_data["points"]:
                    cumtime += penalty
                score += problem_data["points"]
            return score, cumtime

        format_data = calculate_format_data(participation, False)
        score, cumtime = recalculate_results(format_data)

        # Calculate quiz scores using base class method
        quiz_points = self.calculate_quiz_scores(participation, format_data)
        score += quiz_points

        self.handle_frozen_state(participation, format_data)
        participation.cumtime = max(cumtime, 0)
        participation.score = round(score, self.contest.points_precision)
        participation.tiebreaker = 0
        participation.format_data = format_data

        format_data_final = calculate_format_data(participation, True)
        score_final, cumtime_final = recalculate_results(format_data_final)

        # Calculate quiz scores for final as well
        quiz_points_final = self.calculate_quiz_scores(participation, format_data_final)
        score_final += quiz_points_final

        participation.cumtime_final = max(cumtime_final, 0)
        participation.score_final = round(score_final, self.contest.points_precision)
        participation.format_data_final = format_data_final
        participation.save()
