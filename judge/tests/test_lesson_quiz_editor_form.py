"""Lesson editor: the CourseLessonQuiz formset must expose `is_result_hidden`.

The edit-lessons-new page renders CourseLessonQuizForm via the sortable_formset
macro, which auto-renders every visible field. Teachers need to toggle
"Hide Results" per lesson quiz, so the field must be on the form (and persist).
"""

from django.test import TestCase

from judge.models import Course, CourseLesson, Language
from judge.models.quiz import Quiz
from judge.views.course import CourseLessonQuizForm


class CourseLessonQuizFormTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L", content="c", order=1, points=100
        )
        self.quiz = Quiz.objects.create(code="qz1", title="QZ1")

    def _data(self, **overrides):
        data = {
            "order": 1,
            "quiz": self.quiz.id,
            "points": 50,
            "max_attempts": 0,
            "is_visible": "on",
            "lesson": self.lesson.id,
        }
        data.update(overrides)
        return data

    def test_form_exposes_is_result_hidden(self):
        self.assertIn("is_result_hidden", CourseLessonQuizForm.base_fields)

    def test_form_saves_is_result_hidden_true(self):
        form = CourseLessonQuizForm(data=self._data(is_result_hidden="on"))
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertTrue(obj.is_result_hidden)

    def test_form_saves_is_result_hidden_false_when_unchecked(self):
        # Checkbox omitted -> unchecked -> False.
        form = CourseLessonQuizForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertFalse(obj.is_result_hidden)
