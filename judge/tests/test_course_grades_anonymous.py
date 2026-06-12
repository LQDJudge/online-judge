"""Regression test: anonymous access to a course grades page must 404, not 500.

An anonymous user (no profile) hitting a course grades-lesson page used to crash
with `AttributeError: 'NoneType' object has no attribute 'id'` — the view ran to
get_context_data (the access 404 check fires only afterwards), where
_get_unlocked_lessons -> get_lesson_lock_status -> bulk_calculate_lessons_progress
did `[s.id for s in [None]]`. The view now bails out for a None profile, so the
access mixin returns a clean 404.
"""

from django.test import TestCase
from django.urls import reverse

from judge.models import Course, CourseLesson, CourseLessonPrerequisite


class CourseGradesAnonymousTest(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            name="Anon Course",
            slug="anon-course",
            about="about",
            is_public=True,
            is_open=True,
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="content",
            order=2,
            points=100,
        )
        # A real prerequisite makes get_lesson_lock_status compute grades
        # (it short-circuits when there are none) — this is the path that used
        # to crash for a None (anonymous) profile.
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=50,
        )

    def test_anonymous_grades_lesson_returns_404_not_500(self):
        url = reverse("course_grades_lesson", args=[self.course.slug, self.lesson.id])
        response = self.client.get(url)
        # Anonymous is not a course member -> clean 404 (used to be a 500 crash).
        self.assertEqual(response.status_code, 404)
