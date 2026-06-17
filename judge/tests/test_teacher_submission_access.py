"""Course editors (teacher/assistant) can view their students' submissions.

Submission.is_accessible_by grants access when the submission's problem belongs to a
lesson of a course the viewer edits (TEACHER/ASSISTANT, or superuser). Scoped: only
problems in their own courses; submissions outside stay private.

NOTE: this loosens a site-wide access rule — flagged for owner (cuom1999) review.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import (
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
from judge.models.submission import SubmissionSource


class TeacherSubmissionAccessTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="Group")

        self.teacher = self._profile("teacher")
        self.assistant = self._profile("assistant")
        self.student = self._profile("student")
        self.peer = self._profile("peer")  # enrolled student, not owner
        self.other_teacher = self._profile(
            "other_teacher"
        )  # teaches a DIFFERENT course

        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        CourseRole.objects.create(
            course=self.course, user=self.teacher, role=RoleInCourse.TEACHER
        )
        CourseRole.objects.create(
            course=self.course, user=self.assistant, role=RoleInCourse.ASSISTANT
        )
        CourseRole.objects.create(
            course=self.course, user=self.student, role=RoleInCourse.STUDENT
        )
        CourseRole.objects.create(
            course=self.course, user=self.peer, role=RoleInCourse.STUDENT
        )

        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L1", content="c", order=1, points=100
        )
        # Private problem (NOT public) so access can ONLY come from the course-editor
        # rule, not the "solved a public problem" rule.
        self.problem = Problem.objects.create(
            code="p1",
            name="P1",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=False,
        )
        CourseLessonProblem.objects.create(
            lesson=self.lesson, problem=self.problem, order=1, score=100
        )
        self.sub = self._submission(self.student, self.problem)

        # A standalone problem in NO course, with a student submission.
        self.standalone = Problem.objects.create(
            code="p2",
            name="P2",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=False,
        )
        self.standalone_sub = self._submission(self.student, self.standalone)

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return profile

    def _submission(self, profile, problem):
        sub = Submission.objects.create(
            user=profile,
            problem=problem,
            language=self.lang,
            status="D",
            result="AC",
            points=100,
            case_points=100,
            case_total=100,
            time=0.1,
            memory=1024,
        )
        SubmissionSource.objects.create(submission=sub, source="print(1)")
        return sub

    # --- the new course-editor rule ---
    def test_teacher_can_access_student_submission_in_their_course(self):
        self.assertTrue(self.sub.is_accessible_by(self.teacher))

    def test_assistant_can_access_student_submission_in_their_course(self):
        self.assertTrue(self.sub.is_accessible_by(self.assistant))

    # --- scope limits ---
    def test_teacher_cannot_access_submission_outside_their_courses(self):
        # standalone problem isn't in any course this teacher edits
        self.assertFalse(self.standalone_sub.is_accessible_by(self.teacher))

    def test_other_course_teacher_cannot_access(self):
        # other_teacher edits a different course, not this one
        other_course = Course.objects.create(
            name="C2", slug="c2", about="a", is_public=True, is_open=True
        )
        CourseRole.objects.create(
            course=other_course, user=self.other_teacher, role=RoleInCourse.TEACHER
        )
        self.assertFalse(self.sub.is_accessible_by(self.other_teacher))

    def test_peer_student_cannot_access(self):
        # enrolled classmate, not owner, hasn't solved a public problem -> no access
        self.assertFalse(self.sub.is_accessible_by(self.peer))

    def test_owner_can_still_access(self):
        self.assertTrue(self.sub.is_accessible_by(self.student))

    # --- integration: the submission detail page honors the same rule ---
    def test_teacher_can_open_submission_status_page(self):
        self.client.force_login(self.teacher.user)
        resp = self.client.get(reverse("submission_status", args=[self.sub.id]))
        self.assertEqual(resp.status_code, 200)
