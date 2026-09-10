"""Standalone mocked IOI16 regressions, without Django startup or database access.

Run: python3 -B -m unittest judge.tests.test_new_ioi_scoring
The production class is loaded from its AST with only framework dependencies
replaced. The grouped SQL boundary is mocked; backend SQL execution remains an
integration check for the parent/local database test runner.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock


def load_format():
    path = Path(__file__).resolve().parents[1] / "contest_format/new_ioi.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    cls.decorator_list = []
    namespace = {
        "IOIContestFormat": object,
        "gettext_lazy": str,
        "ProblemTestCase": Mock(),
        "connection": MagicMock(),
        "to_database_time": Mock(side_effect=lambda value: value),
        "from_database_time": Mock(side_effect=lambda value: value),
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class NewIOIScoringTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_format()
        self.format = self.namespace["NewIOIContestFormat"]()
        self.start = datetime(2025, 3, 23, 0, tzinfo=timezone.utc)
        self.contest = SimpleNamespace(
            end_time=self.start + timedelta(hours=5),
            freeze_after=None,
            points_precision=2,
            contest_problems=Mock(),
        )
        self.format.contest = self.contest
        self.format.config = {"cumtime": False}
        self.participation = SimpleNamespace(id=123, start=self.start, save=Mock())
        self.configs = []
        self.summaries = []
        self.query = self.namespace["ProblemTestCase"].objects.filter.return_value
        self.query.order_by.return_value.values_list.side_effect = lambda *args: iter(
            self.configs
        )
        self.cursor = self.namespace[
            "connection"
        ].cursor.return_value.__enter__.return_value
        self.cursor.execute.side_effect = self.execute

    def execute(self, sql, parameters):
        self.sql = sql
        self.parameters = parameters
        self.cursor.__iter__.return_value = iter(
            row for row in self.summaries if row[3] < parameters[1]
        )

    def summary(
        self,
        submission,
        batch,
        summed,
        total,
        minimum,
        minute=1,
        problem=10,
        contest_problem=101,
        problem_points=100,
    ):
        return (
            contest_problem,
            problem,
            problem_points,
            self.start + timedelta(minutes=minute),
            submission,
            batch,
            summed,
            total,
            minimum,
        )

    def results(self, include_frozen=False):
        return self.format.get_results_by_subtask(self.participation, include_frozen)

    def test_minimum_scoring_can_change_which_submission_is_best(self):
        self.configs = [(10, "min")]
        # Earlier [1, 0] scores 0; later [.4, .4] scores 40, not 50.
        self.summaries = [
            self.summary(1, 1, 50, 100, 0),
            self.summary(2, 1, 40, 100, 0.4, minute=2),
        ]
        self.assertEqual(
            self.results(),
            [(101, 100, self.start + timedelta(minutes=2), 40, 100, 1, 2)],
        )

    def test_minimum_uses_fraction_and_total_not_minimum_raw_points(self):
        self.configs = [(10, "min")]
        # Unequal maxima 20/80, earned 10/20: fractions .5/.25, group score 25.
        self.summaries = [self.summary(1, 1, 30, 100, 0.25)]
        self.assertEqual(self.results()[0][3:5], (25, 100))

    def test_sum_modes_and_null_unbatched_remain_summed(self):
        self.configs = [(10, "sum"), (10, "min")]
        self.summaries = [
            self.summary(1, None, 10, 20, 0),
            self.summary(2, None, 14, 20, 0, minute=2),
            self.summary(1, 1, 20, 30, 0),
            self.summary(1, 2, 40, 50, 0.5),
        ]
        rows = self.results()
        self.assertEqual(
            [(r[5], r[3], r[6]) for r in rows], [(None, 14, 2), (1, 20, 1), (2, 25, 1)]
        )

    def test_batch_numbers_restart_per_problem_and_count_sum_markers(self):
        self.configs = [(10, "sum"), (10, "min"), (20, "min"), (20, "sum")]
        self.summaries = [
            self.summary(1, 1, 50, 100, 0),
            self.summary(1, 2, 50, 100, 0),
            self.summary(2, 1, 50, 100, 0, problem=20, contest_problem=102),
            self.summary(2, 2, 50, 100, 0, problem=20, contest_problem=102),
        ]
        self.assertEqual([r[3] for r in self.results()], [50, 0, 0, 50])

    def test_zero_total_and_unconfigured_batches_match_bridge_fallback(self):
        self.configs = [(10, "min")]
        self.summaries = [
            self.summary(1, 1, 5, 0, 0),
            self.summary(1, 2, 15, 20, 0),
            self.summary(2, 1, 9, 16, 0, problem=20, contest_problem=102),
        ]
        self.assertEqual([r[3] for r in self.results()], [5, 15, 9])

    def test_zero_weight_child_forces_zero_fraction_when_group_total_positive(self):
        self.configs = [(10, "min")]
        self.summaries = [self.summary(1, 1, 100, 100, 0)]
        self.assertEqual(self.results()[0][3], 0)
        self.assertIn("MIN(CASE WHEN tc.total = 0 THEN 0.0", self.sql)
        self.assertIn("ELSE tc.points / tc.total END)", self.sql)

    def test_equal_best_scores_keep_earliest_time_then_smallest_id(self):
        self.configs = [(10, "min")]
        self.summaries = [
            self.summary(1, 1, 70, 100, 0.5, minute=3),
            self.summary(9, 1, 60, 100, 0.5, minute=2),
            self.summary(8, 1, 50, 100, 0.5, minute=2),
            self.summary(0, 1, 20, 100, 0.2, minute=1),
        ]
        self.assertEqual(
            self.results()[0],
            (101, 100, self.start + timedelta(minutes=2), 50, 100, 1, 8),
        )
        self.summaries.reverse()
        self.assertEqual(self.results()[0][6], 8)

    def test_best_subtasks_can_come_from_different_submissions(self):
        self.configs = [(10, "min"), (10, "min"), (10, "min")]
        self.summaries = [
            self.summary(1, 1, 16, 16, 1),
            self.summary(1, 2, 3, 60, 0.05),
            self.summary(2, 1, 9, 16, 0.5625, minute=2),
            self.summary(2, 2, 57, 60, 0.95, minute=2),
            self.summary(2, 3, 24, 24, 1, minute=2),
        ]
        self.assertEqual(
            [(r[3], r[6]) for r in self.results()], [(16, 1), (57, 2), (24, 2)]
        )

    def test_empty_results_and_batch_loaded_configuration(self):
        self.assertEqual(self.results(), [])
        manager = self.namespace["ProblemTestCase"].objects
        manager.filter.assert_called_once_with(
            dataset_id__in=self.contest.contest_problems.values.return_value,
            type="S",
        )
        self.contest.contest_problems.values.assert_called_once_with("problem_id")
        self.query.order_by.assert_called_once_with("dataset_id", "order")
        self.query.order_by.return_value.values_list.assert_called_once_with(
            "dataset_id", "batch_scoring"
        )
        self.cursor.execute.assert_called_once()

    def test_many_submissions_do_not_add_configuration_queries(self):
        self.configs = [(10, "min")]
        self.summaries = [
            self.summary(i, 1, i / 10, 100, i / 1000) for i in range(1000)
        ]
        self.assertEqual(self.results()[0][6], 999)
        self.namespace["ProblemTestCase"].objects.filter.assert_called_once()
        self.cursor.execute.assert_called_once()

    def test_completed_only_and_strict_frozen_cutoffs(self):
        self.contest.freeze_after = timedelta(minutes=60)
        self.summaries = [
            self.summary(1, None, 20, 100, 0.2, minute=59),
            self.summary(2, None, 40, 100, 0.4, minute=60),
            self.summary(3, None, 60, 100, 0.6, minute=299),
            self.summary(4, None, 100, 100, 1, minute=300),
        ]
        self.assertEqual(self.results()[0][6], 1)
        self.assertEqual(self.parameters, (123, self.start + timedelta(minutes=60)))
        self.assertIn("sub.status = 'D'", self.sql)
        self.assertIn("WHERE sub.date < %s", self.sql)
        self.assertEqual(self.results(include_frozen=True)[0][6], 3)
        self.assertEqual(self.parameters, (123, self.contest.end_time))

    def test_hidden_frozen_normalization_and_cumulative_time_are_preserved(self):
        self.configs = [(10, "min"), (10, "sum")]
        self.contest.freeze_after = timedelta(minutes=60)
        self.contest.contest_problems.values_list.return_value = [(101, "2,invalid")]
        self.format.config["cumtime"] = True
        self.summaries = [
            self.summary(1, 1, 35, 50, 0.4, minute=10, problem_points=200),
            self.summary(1, 2, 30, 50, 0, minute=10, problem_points=200),
            self.summary(2, 1, 50, 50, 1, minute=70, problem_points=200),
            self.summary(2, 2, 50, 50, 1, minute=70, problem_points=200),
        ]
        self.format.compute_score = Mock(
            side_effect=lambda data: sum(r["points"] for r in data.values())
        )
        self.format.compute_cumtime = Mock(
            side_effect=lambda data: sum(r["time"] for r in data.values())
        )
        for name in (
            "calculate_quiz_scores",
            "handle_frozen_state",
            "apply_result_hidden",
        ):
            setattr(self.format, name, Mock())
        self.format.update_participation(self.participation)
        self.assertEqual(self.participation.score, 40)
        self.assertEqual(self.participation.score_final, 200)
        self.assertEqual(self.participation.cumtime, 600)
        self.assertEqual(self.participation.cumtime_final, 4200)
        self.assertEqual(self.participation.format_data["101"]["total_points"], 100)
        self.assertEqual(self.participation.format_data_final["101"]["points"], 200)
        self.assertEqual(self.format.calculate_quiz_scores.call_count, 2)
        self.format.handle_frozen_state.assert_called_once()
        self.format.apply_result_hidden.assert_called_once()
        self.participation.save.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
