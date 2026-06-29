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
    Contest,
    ContestParticipation,
    ContestProblem,
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

    def test_hidden_result_masks_attempt_scores(self):
        self.lesson_quiz.is_result_hidden = True
        self.lesson_quiz.save(update_fields=["is_result_hidden"])

        self.client.force_login(self.student.user)
        resp = self.client.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "lightbox-submissions")
        self.assertContains(resp, "?")
        self.assertNotContains(resp, "case-AC")
        self.assertNotContains(resp, "Best score")
        self.assertContains(resp, "/result/")

    def test_generic_result_url_respects_hidden_lesson_quiz(self):
        self.lesson_quiz.is_result_hidden = True
        self.lesson_quiz.save(update_fields=["is_result_hidden"])
        self.quiz.is_public = True
        self.quiz.save(update_fields=["is_public"])

        self.client.force_login(self.student.user)
        resp = self.client.get(
            reverse("quiz_result", args=[self.quiz.code, self.attempt.id])
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "fa-check-circle")
        self.assertNotContains(resp, "Your Answers")

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

    def test_contest_editor_access_uses_attempt_contest(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="realquizcontest",
            name="Real Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=1
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )

        other_contest = Contest.objects.create(
            key="otherquizcontest",
            name="Other Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        other_contest.authors.add(self.viewer)
        other_contest._author_ids.dirty(other_contest)
        ContestProblem.objects.create(
            contest=other_contest, quiz=self.quiz, points=100, order=1
        )

        self.assertFalse(attempt.is_accessible_by(self.viewer.user))

        editable_contest = Contest.objects.create(
            key="editablequizcontest",
            name="Editable Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        editable_contest.authors.add(self.viewer)
        editable_contest._author_ids.dirty(editable_contest)
        ContestProblem.objects.create(
            contest=editable_contest, quiz=self.quiz, points=100, order=1
        )
        editable_participation = ContestParticipation.objects.create(
            contest=editable_contest, user=self.student
        )
        editable_attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=editable_participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        self.assertTrue(editable_attempt.is_accessible_by(self.viewer.user))

    def test_contest_quiz_popup_result_link_uses_attempt_access(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="popupquizcontest",
            name="Popup Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=1
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        url = reverse(
            "contest_quiz_attempts_ajax",
            args=[contest.key, participation.id, self.quiz.id],
        )
        result_url = reverse("quiz_result", args=[self.quiz.code, attempt.id])

        self.client.force_login(self.viewer.user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, result_url)

        editable_contest = Contest.objects.create(
            key="editablepopupquizcontest",
            name="Editable Popup Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        editable_contest.authors.add(self.viewer)
        editable_contest._author_ids.dirty(editable_contest)
        ContestProblem.objects.create(
            contest=editable_contest, quiz=self.quiz, points=100, order=1
        )
        editable_participation = ContestParticipation.objects.create(
            contest=editable_contest, user=self.student
        )
        editable_attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=editable_participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        editable_url = reverse(
            "contest_quiz_attempts_ajax",
            args=[editable_contest.key, editable_participation.id, self.quiz.id],
        )
        editable_result_url = reverse(
            "quiz_result", args=[self.quiz.code, editable_attempt.id]
        )

        resp = self.client.get(editable_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, editable_result_url)

    def test_contest_quiz_popup_requires_quiz_in_contest(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="popupquizcontestmissingquiz",
            name="Popup Quiz Contest Missing Quiz",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=1
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        url = reverse(
            "contest_quiz_attempts_ajax",
            args=[contest.key, participation.id, self.other_quiz.id],
        )

        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_contest_quiz_popup_requires_contest_access(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="privatepopupquizcontest",
            name="Private Popup Quiz Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
            is_private=True,
        )
        ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=1
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            contest_participation=participation,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        url = reverse(
            "contest_quiz_attempts_ajax",
            args=[contest.key, participation.id, self.quiz.id],
        )

        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_hidden_lesson_404_for_non_editor(self):
        self.lesson.is_visible = False
        self.lesson.save()
        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
