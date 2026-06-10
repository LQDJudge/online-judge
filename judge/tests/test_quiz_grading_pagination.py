"""Regression tests for numbered pagination on the quiz grading views.

GradingDashboard and QuizGradingTab use DiggPaginatorMixin so their page links
render as numbers (1 2 3 ... « ») like the rest of the site, not just prev/next.
The numbered links are produced by templates/list-pages.html iterating
`page_obj.page_range`, which only exists on DiggPaginator's page object (the
default Django Paginator does not provide it).

RED before the fix (the two grading views lacked DiggPaginatorMixin, so only
« » showed); GREEN after adding it.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Language, Profile
from judge.models.quiz import Quiz, QuizAttempt, QuizQuestion, QuizQuestionAssignment


class GradingPaginationTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        lang = Language.objects.first()
        self.admin = User.objects.create_superuser("pageadmin", "a@a.com", "pw")
        Profile.objects.get_or_create(user=self.admin, defaults={"language": lang})

        student_user = User.objects.create_user("pagestudent", "s@s.com", "pw")
        self.student, _ = Profile.objects.get_or_create(
            user=student_user, defaults={"language": lang}
        )

        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Q",
            content="?",
            choices=[{"id": "a", "text": "1"}],
            correct_answers={"answers": "a"},
        )
        self.quiz = Quiz.objects.create(code="pagequiz", title="Page Quiz")
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=question, points=5, order=1
        )

        # 55 submitted attempts -> 2 pages (paginate_by = 50).
        for n in range(1, 56):
            QuizAttempt.objects.create(
                user=self.student,
                quiz=self.quiz,
                attempt_number=n,
                is_submitted=True,
                end_time=timezone.now(),
                max_score=Decimal(5),
            )

        self.client.force_login(self.admin)

    def _assert_numbered_pagination(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # More than one page exists...
        self.assertGreater(response.context["paginator"].num_pages, 1)
        # ...and the page object exposes page_range (DiggPaginator), which
        # list-pages.html needs to render numeric links.
        self.assertTrue(hasattr(response.context["page_obj"], "page_range"))
        # The rendered HTML shows a numbered link (current page marked active),
        # not just the prev/next arrows.
        self.assertContains(response, "active-page")

    def test_grading_dashboard_numbered_pagination(self):
        self._assert_numbered_pagination(reverse("grading_dashboard"))

    def test_quiz_grade_tab_numbered_pagination(self):
        self._assert_numbered_pagination(
            reverse("quiz_grade_tab", args=[self.quiz.code])
        )
