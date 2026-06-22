"""Lesson grades page: the AJAX endpoint behind the quiz score-cell popup.

Clicking a quiz column cell opens a popup listing that student's quiz attempts for the
lesson quiz (time / score / %), mirroring the contest quiz-attempts popup. Anonymous ->
login; non-member -> 403; quiz not in lesson -> 404; the result link is shown only to
the attempt owner or a quiz editor (same gate as LessonQuizResult).
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Course,
    CourseLesson,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Profile,
)
from judge.models.course import RoleInCourse
from judge.models.quiz import Quiz, QuizAttempt


class LessonQuizPopupTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.viewer = self._profile("viewer")  # enrolled, not owner, not editor
        self.student = self._profile("student")  # enrolled, has attempts
        self.outsider = self._profile("outsider")  # NOT enrolled

        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        CourseRole.objects.create(
            course=self.course, user=self.viewer, role=RoleInCourse.STUDENT
        )
        CourseRole.objects.create(
            course=self.course, user=self.student, role=RoleInCourse.STUDENT
        )

        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L1", content="c", order=1, points=100
        )
        self.quiz = Quiz.objects.create(code="lq1", title="LQ1")
        self.lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.lesson, quiz=self.quiz, is_visible=True
        )
        self.attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )

        # A second lesson + lesson_quiz, to test "quiz not in this lesson".
        self.other_lesson = CourseLesson.objects.create(
            course=self.course, title="L2", content="c", order=2, points=100
        )
        self.other_quiz = Quiz.objects.create(code="lq2", title="LQ2")
        self.other_lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.other_lesson, quiz=self.other_quiz, is_visible=True
        )

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return profile

    def _url(self, target=None, lesson_quiz=None, lesson=None):
        return reverse(
            "course_lesson_user_quiz_attempts_ajax",
            args=[
                self.course.slug,
                (lesson or self.lesson).id,
                (target or self.student).user.username,
                (lesson_quiz or self.lesson_quiz).id,
            ],
        )

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_enrolled_member_sees_attempts(self):
        # Assert on stable markers, not the score text (numbers are locale-formatted,
        # e.g. "10,0" under some locales). The attempts table + the AC class (score ==
        # max_score) prove the attempt rendered.
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "lightbox-submissions")
        self.assertContains(resp, "case-AC")

    def test_non_member_forbidden(self):
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_quiz_not_in_lesson_404(self):
        # other_lesson_quiz belongs to other_lesson, not self.lesson
        self.client.force_login(self.viewer.user)
        self.assertEqual(
            self.client.get(self._url(lesson_quiz=self.other_lesson_quiz)).status_code,
            404,
        )

    def test_no_attempts_empty_state(self):
        # viewer (enrolled) has no attempts -> no attempts table
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url(target=self.viewer))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "lightbox-submissions")

    def test_result_link_shown_to_owner(self):
        self.client.force_login(self.student.user)  # owner of the attempt
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/result/")

    def test_result_link_hidden_from_non_editor_member(self):
        self.client.force_login(self.viewer.user)  # enrolled, not owner, not editor
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "/result/")

    def test_result_link_shown_to_quiz_editor(self):
        # a course member who is ALSO a quiz author -> editor -> link shown
        editor = self._profile("quizeditor")
        CourseRole.objects.create(
            course=self.course, user=editor, role=RoleInCourse.STUDENT
        )
        self.quiz.authors.add(editor)
        self.client.force_login(editor.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/result/")

    def test_hidden_lesson_404_for_non_editor(self):
        self.lesson.is_visible = False
        self.lesson.save()
        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
