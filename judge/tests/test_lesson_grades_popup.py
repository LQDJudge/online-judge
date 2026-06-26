"""Lesson grades page: the AJAX endpoint behind the problem-score-cell popup.

Anonymous -> login redirect; enrolled member -> sees the student's submissions for
that problem; non-member -> 403; problem not in lesson -> 404; no submissions ->
friendly empty message.
"""

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
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
        self.assertContains(resp, "lightbox-empty")  # friendly empty state

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

    # --- popup caps rows and flags has_more (limit+1, no separate COUNT) ---
    def test_problem_popup_caps_rows_and_flags_has_more(self):
        from judge.views.course import CourseLessonUserSubmissionsAjax

        # setUp already made 1 submission; add 2 more -> 3 total.
        for _ in range(2):
            Submission.objects.create(
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
        self.client.force_login(self.viewer.user)
        with mock.patch.object(CourseLessonUserSubmissionsAjax, "POPUP_LIMIT", 2):
            resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["has_more"])
        self.assertEqual(resp.content.count(b"lightbox-submissions-time"), 2)

    # --- hidden lesson is 404 for a non-editor ---
    def test_hidden_lesson_404_for_non_editor(self):
        self.lesson.is_visible = False
        self.lesson.save()
        self.client.force_login(self.viewer.user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    # --- hidden contest results must be masked in the popup ---------------------
    # If a submission belongs to a contest problem with hidden results, the popup
    # must not leak its verdict/score (mirrors normal submission views, which call
    # mark_hidden_result_submissions). The source link is still kept (option A);
    # the linked detail page does its own masking.

    def _make_hidden_contest_submission(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="hcpop",
            name="HC",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        hidden_problem = Problem.objects.create(
            code="hp1",
            name="HP1",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        CourseLessonProblem.objects.create(
            lesson=self.lesson, problem=hidden_problem, order=2, score=100
        )
        cp = ContestProblem.objects.create(
            contest=contest,
            problem=hidden_problem,
            points=100,
            order=1,
            is_result_hidden=True,
        )
        part = ContestParticipation.objects.create(contest=contest, user=self.student)
        sub = Submission.objects.create(
            user=self.student,
            problem=hidden_problem,
            language=self.lang,
            status="D",
            result="AC",
            points=100,
            case_points=100,
            case_total=100,
            time=0.1,
            memory=1024,
            contest_object=contest,
        )
        # ContestSubmission.save() copies is_result_hidden from the ContestProblem.
        ContestSubmission.objects.create(
            submission=sub, problem=cp, participation=part, points=100
        )
        return hidden_problem, sub

    def test_hidden_contest_submission_masked_for_member(self):
        hidden_problem, sub = self._make_hidden_contest_submission()
        self.client.force_login(self.viewer.user)
        resp = self.client.get(self._url(problem=hidden_problem))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "lightbox-submissions")  # row still rendered
        self.assertNotContains(resp, "case-AC")  # verdict not leaked
        self.assertContains(resp, ">?<")  # score masked

    def test_hidden_contest_submission_keeps_link_for_owner(self):
        hidden_problem, sub = self._make_hidden_contest_submission()
        self.client.force_login(self.student.user)  # owner can open the source
        resp = self.client.get(self._url(problem=hidden_problem))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "case-AC")  # still masked, even for owner
        self.assertContains(resp, ">?<")
        self.assertContains(
            resp, reverse("submission_status", args=[sub.id])
        )  # link kept
