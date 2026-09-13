"""Tests for the self-contained TF schema and data migration."""

import json
from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations import AddField, RunPython
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.utils import timezone

from judge.models import Language, Profile

tf_migration = import_module("judge.migrations.0276_unify_true_false")
audit = tf_migration.audit
conversion_plan = tf_migration.conversion_plan
converted_response = tf_migration.converted_response


class TrueFalseMigrationTestCase(TestCase):
    fixtures = ["language_small"]

    @classmethod
    def setUpTestData(cls):
        cls.before_state = MigrationLoader(connection).project_state(
            [("judge", "0275_quiz_attempt_deadlines")]
        )
        cls.state = cls.before_state.clone()
        for operation in tf_migration.Migration.operations:
            operation.state_forwards("judge", cls.state)
        cls.apps = cls.state.apps
        cls.Question = cls.apps.get_model("judge", "QuizQuestion")
        cls.Answer = cls.apps.get_model("judge", "QuizAnswer")
        cls.Attempt = cls.apps.get_model("judge", "QuizAttempt")
        cls.Best = cls.apps.get_model("judge", "BestQuizAttempt")
        user = User.objects.create_user(username="tf-migration")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": Language.objects.first()}
        )
        cls.profile_id = profile.pk
        quiz = cls.apps.get_model("judge", "Quiz").objects.create(
            code="tfmigration", title="Migration"
        )
        cls.quiz_id = quiz.pk
        course = cls.apps.get_model("judge", "Course").objects.create(
            name="Migration", slug="tfmigration"
        )
        lesson = cls.apps.get_model("judge", "CourseLesson").objects.create(
            course=course, title="Migration", order=1, points=100
        )
        cls.lesson_quiz_id = (
            cls.apps.get_model("judge", "CourseLessonQuiz")
            .objects.create(lesson=lesson, quiz=quiz, points=100)
            .pk
        )

    def migrate_data(self):
        preflight, add_field, convert = tf_migration.Migration.operations
        self.assertIsInstance(preflight, RunPython)
        self.assertIsInstance(add_field, AddField)
        self.assertIsInstance(convert, RunPython)
        preflight.database_forwards(
            "judge",
            SimpleNamespace(connection=connection),
            self.before_state,
            self.before_state,
        )
        # The test database already has the column; exercise conversion with
        # the historical state immediately after AddField, without repeating DDL.
        convert.database_forwards(
            "judge", SimpleNamespace(connection=connection), self.state, self.state
        )

    def old_question(self, **kwargs):
        values = dict(
            question_type="TF",
            title="Old TF",
            content="**Markdown** statement",
            choices=[{"id": "A", "text": "Đúng"}, {"id": "B", "text": "Sai"}],
            correct_answers={"answers": "B"},
            multiple_true_false_score_table=[0, 10, 25, 50, 100],
        )
        values.update(kwargs)
        return self.Question.objects.create(**values)

    def answer(self, question, value, number=1):
        attempt = self.Attempt.objects.create(
            user_id=self.profile_id,
            quiz_id=self.quiz_id,
            lesson_quiz_id=self.lesson_quiz_id,
            attempt_number=number,
            score=7,
            is_submitted=True,
            end_time=timezone.now(),
        )
        return self.Answer.objects.create(
            question=question,
            attempt=attempt,
            answer=value,
            points=7,
            is_correct=False,
            partial_credit="0.70",
            graded_by_id=self.profile_id,
            graded_at=timezone.now(),
        )

    def test_preserves_grades_timestamps_attempts_and_custom_questions(self):
        question = self.old_question()
        original = self.Question.objects.filter(pk=question.pk).values().get()
        answers = [
            self.answer(question, value, index + 1)
            for index, value in enumerate(["A", "B", ""])
        ]
        self.Best.objects.create(
            user_id=self.profile_id,
            lesson_quiz_id=self.lesson_quiz_id,
            attempt_id=answers[0].attempt_id,
            score=7,
        )
        custom = self.old_question(
            content="Shared context",
            choices=[{"id": "X", "text": "First"}, {"id": "Y", "text": "Second"}],
            correct_answers={"answers": {"X": True, "Y": False}},
            multiple_true_false_score_table=[0, 30, 100],
        )
        custom_before = self.Question.objects.filter(pk=custom.pk).values().get()
        before_answers = list(self.Answer.objects.order_by("pk").values())
        before_attempts = list(self.Attempt.objects.order_by("pk").values())
        before_best = list(self.Best.objects.order_by("pk").values())
        self.migrate_data()
        question.refresh_from_db()
        self.assertEqual(question.content, "")
        self.assertEqual(question.choices, [{"id": "A", "text": original["content"]}])
        self.assertEqual(question.correct_answers, {"answers": {"A": False}})
        self.assertEqual(question.multiple_true_false_score_table, [0, 100])
        after = self.Question.objects.filter(pk=question.pk).values().get()
        for field in set(original) - {
            "content",
            "choices",
            "correct_answers",
            "multiple_true_false_score_table",
        }:
            self.assertEqual(after[field], original[field], field)
        for before, after, value in zip(
            before_answers,
            self.Answer.objects.order_by("pk").values(),
            [{"A": True}, {"A": False}, {}],
        ):
            self.assertEqual(json.loads(after.pop("answer")), value)
            before.pop("answer")
            self.assertEqual(before, after)
        self.assertEqual(
            before_attempts, list(self.Attempt.objects.order_by("pk").values())
        )
        self.assertEqual(before_best, list(self.Best.objects.order_by("pk").values()))
        self.assertEqual(
            custom_before, self.Question.objects.filter(pk=custom.pk).values().get()
        )
        self.assertEqual(audit(self.apps, "default"), ([], 0))
        self.migrate_data()  # Conversion is idempotent.

    def test_audit_rejects_unknown_response_before_any_writes(self):
        question = self.old_question()
        self.answer(question, "A")
        self.answer(question, "unknown", 2)
        before_questions = list(self.Question.objects.values())
        before_answers = list(self.Answer.objects.values())
        with self.assertRaisesRegex(ValueError, "unknown saved choice"):
            self.migrate_data()
        self.assertEqual(before_questions, list(self.Question.objects.values()))
        self.assertEqual(before_answers, list(self.Answer.objects.values()))

    def test_choice_truth_mapping_is_explicit_and_order_independent(self):
        for choices, key, expected in [
            (
                [{"id": "false", "text": "False"}, {"id": "true", "text": "True"}],
                "true",
                {"false": False, "true": True},
            ),
            (
                [{"id": "x", "text": "**Sai**"}, {"id": "y", "text": "Đúng"}],
                "x",
                {"x": False, "y": True},
            ),
        ]:
            with self.subTest(choices=choices):
                question = self.old_question(
                    choices=choices, correct_answers={"answers": key}
                )
                self.assertEqual(conversion_plan(question), expected)
        self.assertEqual(converted_response(1, "  ", {"x": False}), "{}")

    def test_ambiguous_or_invalid_questions_abort(self):
        for changes in [
            {"choices": [{"id": "A", "text": "Yes"}, {"id": "B", "text": "No"}]},
            {
                "choices": [
                    {"id": "true", "text": "False"},
                    {"id": "false", "text": "True"},
                ]
            },
            {"choices": [{"id": "A", "text": "True"}, {"id": "A", "text": "False"}]},
            {"correct_answers": {"answers": "unknown"}},
            {"content": ""},
        ]:
            with self.subTest(changes=changes):
                question = self.old_question(**changes)
                with self.assertRaises(ValueError):
                    conversion_plan(question)
