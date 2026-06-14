"""Regression test: anonymous access to a course grades page must not 500.

An anonymous user (no profile) hitting a course grades-lesson page used to crash
with `AttributeError: 'NoneType' object has no attribute 'id'` (grade calc ran with
[None]). Per the project rule, an auth-required route now redirects anonymous users
to login *before* the view runs — so the page can no longer crash for anonymous.
(`_get_unlocked_lessons` also keeps a defensive None-profile guard.)
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

    def test_anonymous_grades_lesson_redirects_to_login_not_500(self):
        url = reverse("course_grades_lesson", args=[self.course.slug, self.lesson.id])
        response = self.client.get(url)
        # Anonymous -> redirected to login (used to be a 500 crash).
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
