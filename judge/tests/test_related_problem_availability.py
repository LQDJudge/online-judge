from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import ProgrammingError
from django.test import SimpleTestCase, override_settings

from judge.utils.problems import get_related_problems


@override_settings(
    USE_ML=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class RelatedProblemAvailabilityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.profile = SimpleNamespace(id=71, user=SimpleNamespace(id=72))
        self.problem = SimpleNamespace(id=73)
        self.visible = patch("judge.utils.problems.Problem.get_visible_problems")
        self.visible.start().return_value = MagicMock()
        self.addCleanup(self.visible.stop)
        self.completed = patch(
            "judge.utils.problems.user_completed_ids", return_value=[]
        )
        self.completed.start()
        self.addCleanup(self.completed.stop)

    def test_missing_recommendation_table_does_not_break_statement(self):
        with patch("judge.utils.problems.VectorStore") as store:
            store.return_value.problem_neighbors.side_effect = ProgrammingError(
                1146, "Table 'dmoj.ml_problem_embedding_tt' doesn't exist"
            )
            with self.assertLogs("judge.utils.problems", level="WARNING"):
                self.assertIsNone(get_related_problems(self.profile, self.problem))

    def test_missing_fallback_table_also_degrades_gracefully(self):
        with patch("judge.utils.problems.VectorStore") as store:
            store.return_value.problem_neighbors.side_effect = [
                [],
                ProgrammingError(1146, "Missing collaborative embedding table"),
            ]
            with self.assertLogs("judge.utils.problems", level="WARNING"):
                self.assertIsNone(get_related_problems(self.profile, self.problem))

    def test_other_sql_errors_are_not_suppressed(self):
        with patch("judge.utils.problems.VectorStore") as store:
            store.return_value.problem_neighbors.side_effect = ProgrammingError(
                1054, "Unknown column"
            )
            with self.assertRaises(ProgrammingError):
                get_related_problems(self.profile, self.problem)

    def test_available_recommendations_are_preserved(self):
        with patch("judge.utils.problems.VectorStore") as store, patch(
            "judge.utils.problems.Problem.get_cached_instances",
            return_value=[self.problem],
        ) as loader:
            store.return_value.problem_neighbors.return_value = [(0.9, 73)]
            self.assertEqual(
                get_related_problems(self.profile, self.problem), [self.problem]
            )
            loader.assert_called_once_with(73)
