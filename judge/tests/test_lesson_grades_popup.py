"""Lesson grades page: the AJAX endpoint behind the problem-score-cell popup.

Anonymous -> login redirect; enrolled member -> sees the student's submissions for
that problem; non-member -> 403; problem not in lesson -> 404; no submissions ->
friendly empty message.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    BestSubmission,
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Course,
    CourseLesson,
    CourseLessonProblem,
    CourseRole,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)
from judge.models.course import RoleInCourse


class LessonGradesPopupTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="Group")

        self.viewer = self._make_profile("viewer")  # enrolled
        self.student = self._make_profile("student")  # enrolled, has a submission
        self.outsider = self._make_profile("outsider")  # NOT enrolled

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
        CourseLessonProblem.objects.create(
            lesson=self.lesson, problem=self.problem, order=1, score=100
        )
        self.other_problem = Problem.objects.create(
            code="p2",
            name="P2",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        self.submission = Submission.objects.create(
            user=self.student,
            problem=self.problem,
            language=self.lang,
            status="D",
            result="AC",
            points=100,
            case_points=100,
            case_total=100,
            time=0.1,
            memory=1024,
        )

    def _make_profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return profile

    def _url(self, problem=None, student=None):
        return reverse(
            "course_lesson_user_submissions_ajax",
            args=[
                self.course.slug,
                self.lesson.id,
                (student or self.student).user.username,
                (problem or self.problem).code,
            ],
        )

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_enrolled_member_sees_submissions(self):
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "AC")

    def test_non_member_forbidden(self):
        self.client.force_login(self.outsider.user)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_problem_not_in_lesson_404(self):
        self.client.force_login(self.viewer.user)
        self.assertEqual(
            self.client.get(self._url(problem=self.other_problem)).status_code, 404
        )

    def test_no_submissions_empty_state(self):
        # viewer (enrolled) has no submissions to the problem -> no submissions table.
        # Assert on the stable CSS class, not the translatable "No submissions yet."
        # string (which renders in Vietnamese once translations are compiled).
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url(student=self.viewer))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "lightbox-submissions")

    # --- source-link gating (shown only to viewers who can open the submission) ---
    def test_source_link_shown_to_owner(self):
        self.client.force_login(self.student.user)  # owner of the submission
        resp = self.client.get(self._url())
        self.assertContains(
            resp, reverse("submission_status", args=[self.submission.id])
        )

    def test_source_link_hidden_from_non_privileged_member(self):
        # viewer is enrolled but not owner, hasn't solved the problem, no perms
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "lightbox-submissions")  # list still shows
        self.assertNotContains(
            resp, reverse("submission_status", args=[self.submission.id])
        )

    def test_hidden_contest_result_is_masked(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="popuphidden",
            name="Popup Hidden",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        contest_problem = ContestProblem.objects.create(
            contest=contest,
            problem=self.problem,
            points=100,
            order=1,
            is_result_hidden=True,
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        self.submission.contest_object = contest
        self.submission.save(update_fields=["contest_object"])
        ContestSubmission.objects.create(
            submission=self.submission,
            problem=contest_problem,
            participation=participation,
            points=100,
        )

        self.client.force_login(self.student.user)
        resp = self.client.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "?")
        self.assertNotContains(resp, "AC")
        self.assertNotContains(resp, "100 / 100")
        self.assertContains(
            resp, reverse("submission_status", args=[self.submission.id])
        )

    def test_hidden_contest_best_submission_does_not_leak_in_course_grades(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="gradehidden",
            name="Grade Hidden",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        contest_problem = ContestProblem.objects.create(
            contest=contest,
            problem=self.problem,
            points=100,
            order=1,
            is_result_hidden=True,
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student
        )
        self.submission.contest_object = contest
        self.submission.save(update_fields=["contest_object"])
        ContestSubmission.objects.create(
            submission=self.submission,
            problem=contest_problem,
            participation=participation,
            points=100,
        )
        BestSubmission.objects.create(
            user=self.student,
            problem=self.problem,
            submission=self.submission,
            points=100,
            case_total=100,
        )

        self.client.force_login(self.student.user)

        lesson_grade_url = reverse(
            "course_grades_lesson", args=[self.course.slug, self.lesson.id]
        )
        resp = self.client.get(lesson_grade_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertRegex(
            html,
            r'data-featherlight="[^"]*/submissions/[^"]*"[^>]*>\s*\?\s*</a>',
        )
        self.assertNotRegex(
            html,
            r'data-featherlight="[^"]*/submissions/[^"]*"[^>]*>\s*100\s*</a>',
        )

        course_resp = self.client.get(reverse("course_grades", args=[self.course.slug]))
        self.assertEqual(course_resp.status_code, 200)
        self.assertRegex(
            course_resp.content.decode(),
            r'href="'
            + lesson_grade_url
            + r"\?focus="
            + self.student.username
            + r'">\s*0\s*</a>',
        )

        lesson_resp = self.client.get(
            reverse("course_lesson_detail", args=[self.course.slug, self.lesson.id])
        )
        self.assertEqual(lesson_resp.status_code, 200)
        self.assertContains(lesson_resp, "? / 100")
        self.assertNotContains(lesson_resp, "100.0 / 100")

    # --- hidden lesson is 404 for a non-editor ---
    def test_hidden_lesson_404_for_non_editor(self):
        self.lesson.is_visible = False
        self.lesson.save()
        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
