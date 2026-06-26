"""Course grades must mask hidden lesson-PROBLEM results (mirrors the quiz feature).

A CourseLessonProblem with is_result_hidden=True must, uniformly for all roles:
  - show "?" in the lesson-grades problem cell (kept clickable; popup masks for students),
  - be EXCLUDED from the Total / lesson total,
  - show "?" instead of the score on the student's own lesson page.

Data: one visible problem (AC, 100) + one hidden problem (AC, 100). With the hidden one
excluded, the lesson total_points = 100 (only the visible problem) for everyone.
"""

from unittest import mock

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
from judge.models.submission import BestSubmission


class CourseGradesHiddenProblemTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="G")
        self.student = self._profile("stud")
        self.teacher = self._profile("teach")

        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        CourseRole.objects.create(
            course=self.course, user=self.student, role=RoleInCourse.STUDENT
        )
        CourseRole.objects.create(
            course=self.course, user=self.teacher, role=RoleInCourse.TEACHER
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L", content="c", order=1, points=100
        )
        self.vis = self._problem("pvis", order=1, hidden=False)
        self.hid = self._problem("phid", order=2, hidden=True)

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def _problem(self, code, order, hidden):
        problem = Problem.objects.create(
            code=code,
            name=code.upper(),
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        CourseLessonProblem.objects.create(
            lesson=self.lesson,
            problem=problem,
            order=order,
            score=100,
            is_result_hidden=hidden,
        )
        sub = Submission.objects.create(
            user=self.student,
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
        BestSubmission.update_from_submission(sub)
        return problem

    def _grades_for(self, viewer):
        self.client.force_login(viewer.user)
        resp = self.client.get(
            reverse("course_grades_lesson", args=[self.course.slug, self.lesson.id])
        )
        self.assertEqual(resp.status_code, 200)
        g = next(
            v for k, v in resp.context["grades"].items() if k.id == self.student.id
        )
        return resp, g

    def test_lesson_grades_masks_hidden_problem_for_student(self):
        resp, g = self._grades_for(self.student)
        self.assertEqual(g["total"]["total_points"], 100)  # hidden problem excluded
        self.assertContains(resp, "problem-score-hidden")

    def test_lesson_grades_masks_hidden_problem_for_teacher_too(self):
        # Uniform: teacher sees the same masked ranking.
        resp, g = self._grades_for(self.teacher)
        self.assertEqual(g["total"]["total_points"], 100)
        self.assertContains(resp, "problem-score-hidden")

    def test_course_grades_excludes_hidden_problem(self):
        self.client.force_login(self.student.user)
        resp = self.client.get(reverse("course_grades", args=[self.course.slug]))
        self.assertEqual(resp.status_code, 200)
        gl = resp.context["grade_lessons"]
        prog = next(v for k, v in gl.items() if k.id == self.student.id)[self.lesson.id]
        self.assertEqual(prog["total_points"], 100)  # hidden problem excluded

    def test_lesson_view_masks_hidden_problem(self):
        self.client.force_login(self.student.user)
        resp = self.client.get(
            reverse("course_lesson_detail", args=[self.course.slug, self.lesson.id])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "problem-score-hidden")
        lps = {x["problem"].id: x["hidden"] for x in resp.context["lesson_problems"]}
        self.assertTrue(lps[self.hid.id])
        self.assertFalse(lps[self.vis.id])

    # --- submission popup (CourseLessonUserSubmissionsAjax) --------------------
    def _popup(self, problem):
        return reverse(
            "course_lesson_user_submissions_ajax",
            args=[
                self.course.slug,
                self.lesson.id,
                self.student.user.username,
                problem.code,
            ],
        )

    def test_problem_popup_masked_for_student(self):
        # Same shape as the quiz popup: list the rows with "?", but suppress the
        # source link (the submission detail page can't mask a lesson-hidden problem).
        self.client.force_login(self.student.user)
        resp = self.client.get(self._popup(self.hid))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_result_hidden"])
        self.assertContains(resp, "lightbox-submissions")  # rows ARE listed
        self.assertContains(resp, ">?<")  # score masked
        self.assertNotContains(resp, "case-AC")  # verdict not shown
        self.assertNotContains(resp, "/submission/")  # link suppressed (no leak)

    def test_visible_problem_popup_not_masked_for_student(self):
        self.client.force_login(self.student.user)
        resp = self.client.get(self._popup(self.vis))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "case-AC")  # visible problem shows verdict

    def test_problem_popup_shown_to_teacher(self):
        self.client.force_login(self.teacher.user)
        resp = self.client.get(self._popup(self.hid))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "case-AC")  # editor sees the real verdict

    # --- prerequisite unlocking must reflect REAL work, ignoring the hide flag ----
    def _all_hidden_lesson(self, code, order, solved):
        """A lesson whose only graded item is hidden; student has an AC sub iff solved."""
        lesson = CourseLesson.objects.create(
            course=self.course, title=code, content="c", order=order, points=100
        )
        problem = Problem.objects.create(
            code=code,
            name=code.upper(),
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        CourseLessonProblem.objects.create(
            lesson=lesson, problem=problem, order=1, score=100, is_result_hidden=True
        )
        if solved:
            sub = Submission.objects.create(
                user=self.student,
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
            BestSubmission.update_from_submission(sub)
        return lesson

    def test_prerequisite_gate_counts_solved_hidden_problem(self):
        # Student solved the (hidden) problem -> the prerequisite gate must see 100%,
        # NOT 0%. The hide flag affects display, never whether the work counts.
        from judge.utils.course_prerequisites import calculate_user_lesson_grades

        lesson = self._all_hidden_lesson("solvedhid", order=9, solved=True)
        grades = calculate_user_lesson_grades(self.student, [lesson])
        self.assertEqual(grades[lesson.order], 100)

    def test_prerequisite_gate_still_blocks_unsolved_hidden_problem(self):
        # Student did NOT solve it -> gate stays 0% (hiding results must not let
        # students skip prerequisites).
        from judge.utils.course_prerequisites import calculate_user_lesson_grades

        lesson = self._all_hidden_lesson("unsolvedhid", order=10, solved=False)
        grades = calculate_user_lesson_grades(self.student, [lesson])
        self.assertEqual(grades[lesson.order], 0)

    # --- shared helper for "which problems are hidden" (#4) --------------------
    def test_hidden_lesson_problem_ids_helper(self):
        from judge.views.course import hidden_lesson_problem_ids

        ids = hidden_lesson_problem_ids(self.lesson.get_problems_and_scores())
        self.assertIn(self.hid.id, ids)
        self.assertNotIn(self.vis.id, ids)

    # --- hidden flag must be keyed by problem id, not list position (#2) -------
    def test_lesson_view_hidden_flag_keyed_by_id_when_get_problems_drops_one(self):
        # get_problems() (Problem.get_cached_instances) drops problems whose cache is
        # missing, so it can be SHORTER than get_problems_and_scores(). A positional
        # zip would then attach the wrong is_result_hidden flag. Simulate by making
        # get_problems() return only the hidden problem; the flag must still follow id.
        self.client.force_login(self.student.user)
        with mock.patch.object(CourseLesson, "get_problems", return_value=[self.hid]):
            resp = self.client.get(
                reverse("course_lesson_detail", args=[self.course.slug, self.lesson.id])
            )
        self.assertEqual(resp.status_code, 200)
        lps = {x["problem"].id: x["hidden"] for x in resp.context["lesson_problems"]}
        self.assertTrue(lps[self.hid.id])  # the hidden problem stays flagged hidden
