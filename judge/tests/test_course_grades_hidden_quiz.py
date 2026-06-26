"""Course grades must mask hidden lesson-quiz results (mirrors contest behaviour).

A lesson quiz with is_result_hidden=True must, for non-editors:
  - show "?" in the lesson-grades quiz cell (kept clickable; the popup masks too),
  - be EXCLUDED from the Total %/lesson total (so it can't be inferred by subtraction),
  - show "?" instead of best_score on the student's own lesson page.
Editors (teachers) still see the real score everywhere.

Data: one visible problem (AC, 100) + one hidden quiz (points 50, max 10, score 8 ->
achieved 40). Teacher total = (100+40)/(100+50) = 93%; student total = 100/100 = 100%.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Course,
    CourseLesson,
    CourseLessonProblem,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)
from judge.models.course import RoleInCourse
from judge.models.quiz import (
    BestQuizAttempt,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionAssignment,
)
from judge.models.submission import BestSubmission


class CourseGradesHiddenQuizTest(TestCase):
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

        # Visible problem with a full-score submission.
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
        sub = Submission.objects.create(
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
        BestSubmission.update_from_submission(sub)

        # Hidden quiz: 1 question (10 pts), attempt score 8 -> achieved 40 of its 50.
        self.quiz = Quiz.objects.create(code="q1", title="Q1")
        qq = QuizQuestion.objects.create(
            question_type="TF",
            title="t",
            content="c",
            choices=[{"id": "true", "text": "True"}, {"id": "false", "text": "False"}],
            correct_answers={"answers": "true"},
            is_public=True,
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=qq, points=10, order=1
        )
        self.lq = CourseLessonQuiz.objects.create(
            lesson=self.lesson,
            quiz=self.quiz,
            is_visible=True,
            is_result_hidden=True,
            points=50,
            order=1,
        )
        QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            lesson_quiz=self.lq,
            attempt_number=1,
            is_submitted=True,
            score=Decimal("8"),
            max_score=Decimal("10"),
            end_time=timezone.now(),
        )
        BestQuizAttempt.recalculate_for_user_lesson_quiz(self.student.id, self.lq.id)

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def _lesson_grades_url(self):
        return reverse("course_grades_lesson", args=[self.course.slug, self.lesson.id])

    def _grades_for(self, viewer):
        # Assert on resp.context (the computed grade dict) + stable CSS markers,
        # NOT on formatted numbers ("40.0" -> "40,0" once vi translations compile).
        self.client.force_login(viewer.user)
        resp = self.client.get(self._lesson_grades_url())
        self.assertEqual(resp.status_code, 200)
        grades = resp.context["grades"]
        g = next(v for k, v in grades.items() if k.id == self.student.id)
        return resp, g

    # --- Piece 1: lesson grades page -------------------------------------------
    def test_lesson_grades_excludes_hidden_quiz_for_student(self):
        resp, g = self._grades_for(self.student)
        # Quiz (50 pts) excluded from the total -> only the problem (100) counts.
        self.assertEqual(g["total"]["total_points"], 100)
        self.assertEqual(g["total"]["achieved_points"], 100)
        self.assertTrue(g[f"quiz_{self.lq.id}"].get("hidden"))
        self.assertContains(resp, "quiz-score-hidden")  # cell renders masked marker

    def test_lesson_grades_masks_quiz_for_teacher_too(self):
        # Uniform view: the grades ranking looks the SAME for teachers as students.
        # Teachers recover the real score only via the popup (viewer-aware), not here.
        resp, g = self._grades_for(self.teacher)
        self.assertEqual(g["total"]["total_points"], 100)  # quiz excluded for all
        self.assertTrue(g[f"quiz_{self.lq.id}"].get("hidden"))
        self.assertContains(resp, "quiz-score-hidden")

    # --- Piece 2: course grades page (per-lesson totals) -----------------------
    def _course_lesson_progress_for(self, viewer):
        self.client.force_login(viewer.user)
        resp = self.client.get(reverse("course_grades", args=[self.course.slug]))
        self.assertEqual(resp.status_code, 200)
        gl = resp.context["grade_lessons"]
        student_lessons = next(v for k, v in gl.items() if k.id == self.student.id)
        return student_lessons[self.lesson.id]

    def test_course_grades_excludes_hidden_quiz_for_student(self):
        prog = self._course_lesson_progress_for(self.student)
        self.assertEqual(prog["total_points"], 100)  # quiz (50) excluded

    def test_course_grades_masks_quiz_for_teacher_too(self):
        prog = self._course_lesson_progress_for(self.teacher)
        self.assertEqual(prog["total_points"], 100)  # quiz excluded for all

    # --- Piece 3: student's own lesson view (lesson.html) ----------------------
    def _lesson_quiz_item(self, viewer):
        self.client.force_login(viewer.user)
        resp = self.client.get(
            reverse("course_lesson_detail", args=[self.course.slug, self.lesson.id])
        )
        self.assertEqual(resp.status_code, 200)
        item = next(
            q
            for q in resp.context["lesson_quizzes"]
            if q["lesson_quiz"].id == self.lq.id
        )
        return resp, item

    def test_lesson_view_masks_quiz_score_for_student(self):
        resp, item = self._lesson_quiz_item(self.student)
        self.assertTrue(item["hidden"])
        self.assertContains(resp, "quiz-score-hidden")

    def test_lesson_view_masks_quiz_for_teacher_too(self):
        resp, item = self._lesson_quiz_item(self.teacher)
        self.assertTrue(item["hidden"])
        self.assertContains(resp, "quiz-score-hidden")
