"""Lesson grades page: the AJAX endpoint behind the quiz score-cell popup.

Clicking a quiz column cell opens a popup listing that student's quiz attempts for the
lesson quiz (time / score / %), mirroring the contest quiz-attempts popup. Anonymous ->
login; non-member -> 403; quiz not in lesson -> 404; the result link is shown only to
the attempt owner or a quiz editor (same gate as LessonQuizResult).
"""

from decimal import Decimal
from unittest import mock

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
from judge.views.course import CourseLessonUserQuizAttemptsAjax


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
        self.assertContains(resp, "lightbox-empty")  # friendly empty state

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

    def test_invisible_lesson_quiz_404_for_non_editor(self):
        # CourseLessonQuiz.is_visible=False must hide the quiz from non-editors,
        # mirroring the lesson-visibility gate.
        self.lesson_quiz.is_visible = False
        self.lesson_quiz.save()
        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_popup_survives_attempt_with_null_score(self):
        # An older/partial submitted attempt can have score=None while max_score is set.
        # The "%" cell divides score/max_score; it must not crash the whole popup.
        QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            attempt_number=2,
            is_submitted=True,
            score=None,
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        self.client.force_login(self.viewer.user)  # non-editor, quiz not hidden
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_invisible_lesson_quiz_visible_to_editor(self):
        self.lesson_quiz.is_visible = False
        self.lesson_quiz.save()
        teacher = self._profile("vis_teacher")
        CourseRole.objects.create(
            course=self.course, user=teacher, role=RoleInCourse.TEACHER
        )
        self.client.force_login(teacher.user)
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    # --- Hidden results (CourseLessonQuiz.is_result_hidden) ---------------------
    # When a teacher ticks "Hide Results", the score must be masked from students
    # (mirrors the contest quiz popup: "?" instead of the number, no "Best score").
    # The masking rule is Quiz.should_hide_result(user, lesson_quiz=...): visible
    # only to superusers, quiz editors and course editors -- NOT even the owner.

    def _hide_results(self):
        self.lesson_quiz.is_result_hidden = True
        self.lesson_quiz.save()

    def test_score_hidden_from_enrolled_student_when_result_hidden(self):
        self._hide_results()
        self.client.force_login(self.viewer.user)  # enrolled, not owner, not editor
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ">?<")  # masked placeholder
        self.assertNotContains(resp, "case-AC")  # real score cell not rendered

    def test_score_hidden_from_owner_when_result_hidden(self):
        # The owner is NOT special-cased: hidden means hidden from the student too.
        self._hide_results()
        self.client.force_login(self.student.user)  # owner of the attempt
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ">?<")
        self.assertNotContains(resp, "case-AC")

    def test_best_score_hidden_when_result_hidden(self):
        # The "Best score" block (<strong>...) must not render when hidden.
        self._hide_results()
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "<strong>")

    def test_score_shown_to_course_teacher_when_result_hidden(self):
        # A course editor (Teacher) still sees the real score.
        teacher = self._profile("teacher")
        CourseRole.objects.create(
            course=self.course, user=teacher, role=RoleInCourse.TEACHER
        )
        self._hide_results()
        self.client.force_login(teacher.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "case-AC")
        self.assertNotContains(resp, ">?<")

    def test_score_shown_to_quiz_editor_when_result_hidden(self):
        # A quiz author (editor) still sees the real score.
        editor = self._profile("hidqeditor")
        CourseRole.objects.create(
            course=self.course, user=editor, role=RoleInCourse.STUDENT
        )
        self.quiz.authors.add(editor)
        self._hide_results()
        self.client.force_login(editor.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "case-AC")
        self.assertNotContains(resp, ">?<")

    # --- Attempt cap (POPUP_LIMIT) ---------------------------------------------
    # Unlimited-attempt quizzes can accumulate many attempts; the popup must cap
    # the rows shown (like the submission popup) and flag "has more" via limit+1.

    def _add_attempts(self, n):
        for i in range(n):
            QuizAttempt.objects.create(
                user=self.student,
                quiz=self.quiz,
                lesson_quiz=self.lesson_quiz,
                attempt_number=i + 2,  # setUp already created attempt #1
                is_submitted=True,
                score=Decimal("5"),
                max_score=Decimal("10"),
                end_time=timezone.now(),
            )

    def test_quiz_popup_caps_attempt_rows(self):
        self._add_attempts(3)  # 4 submitted attempts total
        self.client.force_login(self.viewer.user)
        with mock.patch.object(CourseLessonUserQuizAttemptsAjax, "POPUP_LIMIT", 2):
            resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        # One "lightbox-submissions-time" cell per rendered attempt row.
        self.assertEqual(resp.content.count(b"lightbox-submissions-time"), 2)
        self.assertContains(resp, "quiz-attempts-more")  # "has more" hint

    def test_quiz_popup_no_more_hint_when_within_limit(self):
        # setUp has a single attempt; default limit is 50 -> no "has more" hint.
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "quiz-attempts-more")
