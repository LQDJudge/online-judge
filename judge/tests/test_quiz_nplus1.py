"""Regression tests guarding against N+1 queries in the quiz grading views.

These assert that the number of queries touching the QuizAnswer table does NOT
grow as more attempts are listed — i.e. the per-attempt `has_ungraded_essays`
flag must be computed with a single batched query, not one query per attempt.

Currently RED: it fails against the per-attempt `.exists()` loop in
GradingDashboard / QuizGradingTab, and turns GREEN once that loop is replaced
with a single batched lookup.
"""

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from judge.models import Language, Profile
from judge.models.quiz import (
    Quiz,
    QuizAnswer,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionAssignment,
)

ANSWER_TABLE = QuizAnswer._meta.db_table
ASSIGNMENT_TABLE = QuizQuestionAssignment._meta.db_table
ATTEMPT_TABLE = QuizAttempt._meta.db_table


class QuizListNPlusOneTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        language = Language.objects.first()
        self.user = User.objects.create_user("quizlistuser", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": language}
        )
        self.client.force_login(self.user)

    def _add_quiz(self, index):
        question = QuizQuestion.objects.create(
            question_type="MC",
            title=f"Question {index}",
            content="Choose the answer",
            choices=[{"id": "A", "text": "Answer"}],
            correct_answers={"answers": "A"},
        )
        quiz = Quiz.objects.create(
            code=f"listquery{index}", title=f"List Query Quiz {index}", is_public=True
        )
        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=question, points=10, order=1
        )
        QuizAttempt.objects.create(
            user=self.profile,
            quiz=quiz,
            attempt_number=1,
            is_submitted=True,
            score=4,
        )
        QuizAttempt.objects.create(
            user=self.profile,
            quiz=quiz,
            attempt_number=2,
            is_submitted=True,
            score=9,
        )
        return quiz

    def _get_list_query_counts(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("quiz_list"))
            self.assertEqual(response.status_code, 200)

        sql = [query["sql"] for query in queries.captured_queries]
        return response, {
            "assignments": sum(ASSIGNMENT_TABLE in query for query in sql),
            "attempts": sum(ATTEMPT_TABLE in query for query in sql),
        }

    def test_quiz_list_stats_do_not_scale_queries_per_quiz(self):
        first_quiz = self._add_quiz(1)
        response, base_counts = self._get_list_query_counts()

        self.assertEqual(response.context["attempt_counts"][first_quiz.id], 2)
        self.assertEqual(response.context["best_scores"][first_quiz.id], 9.0)
        self.assertEqual(response.context["quizzes"][0].question_count, 1)

        for index in range(2, 6):
            self._add_quiz(index)
        response, grown_counts = self._get_list_query_counts()

        self.assertEqual(base_counts, grown_counts)
        self.assertEqual(len(response.context["quizzes"]), 5)


class GradingDashboardNPlusOneTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        lang = Language.objects.first()

        # Superuser views the dashboard (sees every submitted attempt).
        self.admin = User.objects.create_superuser("quizadmin", "a@a.com", "pw")
        Profile.objects.get_or_create(user=self.admin, defaults={"language": lang})

        # A single student owns all attempts, so per-user template lookups
        # (gravatar / link_user) stay constant and don't confound the count.
        student_user = User.objects.create_user("student", "s@s.com", "pw")
        self.student, _ = Profile.objects.get_or_create(
            user=student_user, defaults={"language": lang}
        )

        self.essay = QuizQuestion.objects.create(
            question_type="ES",
            title="Essay",
            content="Explain",
            correct_answers=None,
        )
        self.quiz = Quiz.objects.create(code="nplus1quiz", title="N+1 Quiz")
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.essay, points=10, order=1
        )

        self.client.force_login(self.admin)

    def _add_attempt(self, n):
        """A submitted attempt with one ungraded (graded_at=None) essay answer."""
        attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            attempt_number=n,
            is_submitted=True,
        )
        QuizAnswer.objects.create(
            attempt=attempt,
            question=self.essay,
            answer="an essay answer",
            graded_at=None,
        )
        return attempt

    def _answer_query_count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
        return sum(1 for q in ctx.captured_queries if ANSWER_TABLE in q["sql"])

    def _assert_constant(self, url, label):
        # one attempt currently on the dashboard
        self._add_attempt(1)
        base = self._answer_query_count(url)

        # grow to five attempts
        for n in range(2, 6):
            self._add_attempt(n)
        grown = self._answer_query_count(url)

        self.assertEqual(
            grown,
            base,
            f"N+1 in {label}: {base} {ANSWER_TABLE} queries with 1 attempt, "
            f"{grown} with 5. The per-attempt has_ungraded_essays check must be "
            f"batched into a single query.",
        )

    def test_grading_dashboard_no_nplus1(self):
        self._assert_constant(reverse("grading_dashboard"), "GradingDashboard")

    def test_quiz_grade_tab_no_nplus1(self):
        # QuizGradingTab is the per-quiz twin of the dashboard (same loop).
        url = reverse("quiz_grade_tab", args=[self.quiz.code])
        self._assert_constant(url, "QuizGradingTab")
