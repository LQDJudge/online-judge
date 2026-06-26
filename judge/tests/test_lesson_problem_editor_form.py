"""Lesson editor: the CourseLessonProblem formset must expose `is_result_hidden`.

Teachers need a per-problem "Hide Results" toggle (mirrors the lesson-quiz one), so
the field must be on the form and persist.
"""

from django.test import TestCase

from judge.models import Course, CourseLesson, Language, Problem, ProblemGroup
from judge.views.course import CourseLessonProblemForm


class CourseLessonProblemFormTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="G")
        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L", content="c", order=1, points=100
        )
        self.problem = Problem.objects.create(
            code="p1",
            name="P1",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )

    def _data(self, **overrides):
        data = {
            "order": 1,
            "problem": self.problem.id,
            "score": 100,
            "lesson": self.lesson.id,
        }
        data.update(overrides)
        return data

    def test_form_exposes_is_result_hidden(self):
        self.assertIn("is_result_hidden", CourseLessonProblemForm.base_fields)

    def test_form_saves_is_result_hidden_true(self):
        form = CourseLessonProblemForm(data=self._data(is_result_hidden="on"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().is_result_hidden)

    def test_form_saves_is_result_hidden_false_when_unchecked(self):
        form = CourseLessonProblemForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.save().is_result_hidden)
