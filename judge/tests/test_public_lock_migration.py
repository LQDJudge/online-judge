"""Exercise the rollout with historical models, without changing the app DB."""

from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.db.migrations import RunPython
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

lock_migration = import_module("judge.migrations.0279_lock_existing_public_content")


class PublicLockMigrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.state = MigrationLoader(connection).project_state(
            [("judge", "0278_review_coordinator_schema")]
        )
        cls.apps = cls.state.apps

    def setUp(self):
        self.Problem = self.apps.get_model("judge", "Problem")
        self.Contest = self.apps.get_model("judge", "Contest")
        self.group = self.apps.get_model("judge", "ProblemGroup").objects.create(
            name="migration-lock", full_name="Migration lock"
        )

    def problem(self, code, **fields):
        return self.Problem.objects.create(
            code=code,
            name=code,
            group_id=self.group.pk,
            time_limit=1,
            memory_limit=65536,
            points=100,
            **fields,
        )

    def contest(self, key, **fields):
        return self.Contest.objects.create(
            key=key,
            name=key,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
            **fields,
        )

    def migrate_data(self, reverse=False):
        operation = lock_migration.Migration.operations[0]
        method = (
            operation.database_backwards if reverse else operation.database_forwards
        )
        method("judge", SimpleNamespace(connection=connection), self.state, self.state)

    def test_only_public_problems_and_visible_contests_are_locked(self):
        public = self.problem("public", is_public=True, is_locked=False)
        private = self.problem("private", is_public=False, is_locked=False)
        already_locked = self.problem("locked", is_public=False, is_locked=True)
        visible = self.contest("visible", is_visible=True, is_locked=False)
        hidden = self.contest("hidden", is_visible=False, is_locked=False)
        self.migrate_data()
        for obj, expected in (
            (public, True),
            (private, False),
            (already_locked, True),
            (visible, True),
            (hidden, False),
        ):
            obj.refresh_from_db()
            self.assertEqual(obj.is_locked, expected)
        self.assertEqual(public.name, "public")
        self.assertEqual(public.points, 100)

    def test_quizzes_questions_and_posts_are_untouched(self):
        quiz = self.apps.get_model("judge", "Quiz").objects.create(
            code="publicquiz", title="Public quiz", is_public=True, is_locked=False
        )
        question = self.apps.get_model("judge", "QuizQuestion").objects.create(
            title="Public question",
            content="Text",
            question_type="MC",
            is_public=True,
            is_locked=False,
        )
        post = self.apps.get_model("judge", "BlogPost").objects.create(
            title="Public post",
            content="Text",
            publish_on=timezone.now(),
            visible=True,
            is_locked=False,
        )
        self.migrate_data()
        for obj in (quiz, question, post):
            obj.refresh_from_db()
            self.assertFalse(obj.is_locked)

    def test_contest_membership_does_not_lock_private_problem(self):
        problem = self.problem("privatechild", is_public=False, is_locked=False)
        contest = self.contest("publicparent", is_visible=True, is_locked=False)
        self.apps.get_model("judge", "ContestProblem").objects.create(
            contest_id=contest.pk, problem_id=problem.pk, order=1, points=100
        )
        self.migrate_data()
        contest.refresh_from_db()
        problem.refresh_from_db()
        self.assertTrue(contest.is_locked)
        self.assertFalse(problem.is_public)
        self.assertFalse(problem.is_locked)

    def test_batches_are_committed_and_partial_failure_can_be_resumed(self):
        rows = [
            self.problem("batch%s" % index, is_public=True, is_locked=False)
            for index in range(5)
        ]
        queryset_class = type(self.Problem.objects.all())
        original = queryset_class.update
        calls = []

        def interrupt(queryset, **kwargs):
            if queryset.model._meta.label_lower == "judge.problem" and kwargs == {
                "is_locked": True
            }:
                calls.append(queryset.count())
                if len(calls) == 2:
                    raise RuntimeError("Simulated interrupted deployment")
            return original(queryset, **kwargs)

        self.assertFalse(lock_migration.Migration.atomic)
        with patch.object(lock_migration, "BATCH_SIZE", 2), patch.object(
            queryset_class, "update", interrupt
        ):
            with self.assertRaisesMessage(
                RuntimeError, "Simulated interrupted deployment"
            ):
                self.migrate_data()
        self.assertEqual(calls, [2, 2])
        self.assertEqual(self.Problem.objects.filter(is_locked=True).count(), 2)
        with patch.object(lock_migration, "BATCH_SIZE", 2):
            self.migrate_data()
        self.assertEqual(
            self.Problem.objects.filter(
                pk__in=[row.pk for row in rows], is_locked=True
            ).count(),
            5,
        )
        with CaptureQueriesContext(connection) as queries:
            self.migrate_data()  # One empty query per model, no writes.
        self.assertFalse(
            any(query["sql"].lstrip().upper().startswith("UPDATE") for query in queries)
        )

    def test_migration_executor_applies_once_and_reverse_does_not_unlock(self):
        public = self.problem("executorpublic", is_public=True, is_locked=False)
        executor = MigrationExecutor(connection)
        migration = lock_migration.Migration(
            "0279_lock_existing_public_content", "judge"
        )
        executor.apply_migration(self.state.clone(), migration)
        self.addCleanup(executor.recorder.record_unapplied, "judge", migration.name)
        self.assertIn(("judge", migration.name), executor.recorder.applied_migrations())
        public.refresh_from_db()
        self.assertTrue(public.is_locked)
        executor.unapply_migration(self.state.clone(), migration)
        self.assertNotIn(
            ("judge", migration.name), executor.recorder.applied_migrations()
        )
        public.refresh_from_db()
        self.assertTrue(public.is_locked)

    def test_reverse_is_noop_and_keeps_existing_locks(self):
        public = self.problem("publicreverse", is_public=True, is_locked=False)
        deliberate = self.problem("deliberate", is_public=True, is_locked=True)
        self.migrate_data()
        self.migrate_data(reverse=True)
        public.refresh_from_db()
        deliberate.refresh_from_db()
        self.assertTrue(public.is_locked)
        self.assertTrue(deliberate.is_locked)
        operation = lock_migration.Migration.operations[0]
        self.assertIsInstance(operation, RunPython)
        self.assertIs(operation.reverse_code, RunPython.noop)
