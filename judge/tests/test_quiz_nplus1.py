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
