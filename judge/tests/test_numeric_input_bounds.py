from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from judge.models import Contest, ProblemData
from judge.models.course import (
    CourseContest,
    CourseLesson,
    CourseLessonProblem,
    MAX_COURSE_ITEM_POINTS,
)
from judge.models.quiz import (
    CourseLessonQuiz,
    MAX_LESSON_QUIZ_POINTS,
    MAX_QUIZ_ATTEMPTS,
    MAX_QUIZ_TIME_LIMIT_MINUTES,
    Quiz,
    QuizAttempt,
)
from judge.models.problem_data import (
    MAX_COMMUNICATION_NUM_PROCESSES,
    MAX_OUTPUT_ZIP_SIZE_MB,
)


class NumericInputBoundsTests(SimpleTestCase):
    def assert_field_accepts_and_rejects(self, model, field_name, valid, invalid):
        field = model._meta.get_field(field_name)
        field.clean(valid, None)

        with self.assertRaises(ValidationError):
            field.clean(invalid, None)

    def test_contest_duration_fields_have_explicit_bounds(self):
        now = timezone.now()
        contest = Contest(
            key="numericbounds",
            name="Numeric Bounds",
            start_time=now,
            end_time=now + timedelta(hours=2),
            time_limit=timedelta(hours=3),
        )

        with self.assertRaises(ValidationError):
            contest.clean()

        contest.time_limit = timedelta(hours=1)
        contest.freeze_after = timedelta(seconds=-1)

        with self.assertRaises(ValidationError):
            contest.clean()

    def test_quiz_time_limit_has_upper_bound(self):
        self.assert_field_accepts_and_rejects(
            Quiz,
            "time_limit",
            MAX_QUIZ_TIME_LIMIT_MINUTES,
            MAX_QUIZ_TIME_LIMIT_MINUTES + 1,
        )
        self.assert_field_accepts_and_rejects(
            QuizAttempt,
            "time_limit_minutes",
            MAX_QUIZ_TIME_LIMIT_MINUTES,
            MAX_QUIZ_TIME_LIMIT_MINUTES + 1,
        )

    def test_lesson_quiz_attempts_and_points_have_bounds(self):
        self.assert_field_accepts_and_rejects(
            CourseLessonQuiz, "max_attempts", MAX_QUIZ_ATTEMPTS, MAX_QUIZ_ATTEMPTS + 1
        )
        self.assert_field_accepts_and_rejects(
            CourseLessonQuiz,
            "points",
            MAX_LESSON_QUIZ_POINTS,
            MAX_LESSON_QUIZ_POINTS + 1,
        )

    def test_course_grade_points_have_bounds(self):
        for model, field_name in (
            (CourseLesson, "points"),
            (CourseLessonProblem, "score"),
            (CourseContest, "points"),
        ):
            self.assert_field_accepts_and_rejects(
                model, field_name, MAX_COURSE_ITEM_POINTS, MAX_COURSE_ITEM_POINTS + 1
            )

    def test_problem_data_resource_fields_have_bounds(self):
        self.assert_field_accepts_and_rejects(
            ProblemData,
            "output_zip_size_mb",
            MAX_OUTPUT_ZIP_SIZE_MB,
            MAX_OUTPUT_ZIP_SIZE_MB + 1,
        )
        self.assert_field_accepts_and_rejects(
            ProblemData,
            "communication_num_processes",
            MAX_COMMUNICATION_NUM_PROCESSES,
            MAX_COMMUNICATION_NUM_PROCESSES + 1,
        )
