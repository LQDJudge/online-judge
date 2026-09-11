import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from judge.bridge.judge_handler import _synchronize_terminal_failure
from judge.models import (
    BestQuizAttempt,
    BestSubmission,
    Course,
    CourseLesson,
    CourseLessonPrerequisite,
    CourseLessonProgress,
    CourseLessonProblem,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Quiz,
    QuizAnswer,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionAssignment,
    Submission,
)
from judge.models.course import RoleInCourse
from judge.utils.course_prerequisites import (
    calculate_user_lesson_grades,
    get_lesson_lock_status,
    update_lesson_unlock_states,
)
from judge.utils.problems import finished_submission
from judge.utils.quiz_grading import sync_quiz_attempt_result


class TerminalFailureQueueIsolationTest(SimpleTestCase):
    def test_each_broker_failure_preserves_reconciliation_and_other_task(self):
        for failing_task in ("update_user_points", "update_problem_stats"):
            with self.subTest(failing_task=failing_task), patch(
                "judge.bridge.judge_handler.Submission.objects"
            ) as manager, patch(
                "judge.bridge.judge_handler.update_user_points.delay"
            ) as user_task, patch(
                "judge.bridge.judge_handler.update_problem_stats.delay"
            ) as problem_task, patch(
                "judge.bridge.judge_handler.finished_submission"
            ) as finished, patch(
                "judge.bridge.judge_handler.event.post"
            ) as event_post, patch(
                "judge.bridge.judge_handler.logger.exception"
            ) as log:
                submission = Mock(id=123, user_id=4, problem_id=5)
                submission.contest_object_id = None
                manager.select_related.return_value.get.return_value = submission
                task = (
                    user_task if failing_task == "update_user_points" else problem_task
                )

                def fail_after_reconciliation(*args):
                    submission.update_contest.assert_called_once_with()
                    finished.assert_called_once_with(submission)
                    raise RuntimeError("broker unavailable")

                task.side_effect = fail_after_reconciliation
                _synchronize_terminal_failure(submission.id)

                submission.update_contest.assert_called_once_with()
                finished.assert_called_once_with(submission)
                user_task.assert_called_once_with(4)
                problem_task.assert_called_once_with(5)
                log.assert_called_once()
                event_post.assert_not_called()


class ContestQuizResultSynchronizationTest(SimpleTestCase):
    @patch("judge.utils.quiz_grading.event.post")
    @patch("judge.utils.quiz_grading.apps.get_model")
    def test_submitted_attempt_recomputes_contest_and_posts_event(
        self, get_model, event_post
    ):
        participation = SimpleNamespace(
            recompute_results=Mock(),
            contest=SimpleNamespace(
                scoreboard_visibility="visible", key="sync-contest"
            ),
        )
        best_attempt_model = SimpleNamespace(update_from_attempt=Mock())
        get_model.side_effect = [
            SimpleNamespace(SCOREBOARD_VISIBLE="visible"),
            best_attempt_model,
        ]
        attempt = SimpleNamespace(
            id=123,
            is_submitted=True,
            contest_participation_id=456,
            contest_participation=participation,
        )

        sync_quiz_attempt_result(attempt)

        participation.recompute_results.assert_called_once_with()
        best_attempt_model.update_from_attempt.assert_called_once_with(attempt)
        event_post.assert_called_once_with(
            "contest_sync-contest", {"type": "ranking-update"}
        )


class QuizResultSynchronizationTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.first()
        self.student_user = User.objects.create_user("sync_student", password="pw")
        self.student, _ = Profile.objects.get_or_create(
            user=self.student_user, defaults={"language": self.language}
        )
        self.teacher_user = User.objects.create_superuser(
            "sync_teacher", "teacher@example.com", "pw"
        )
        self.teacher, _ = Profile.objects.get_or_create(
            user=self.teacher_user, defaults={"language": self.language}
        )
        self.course = Course.objects.create(
            name="Synchronization course",
            slug="synchronization-course",
            about="Test",
            is_public=True,
            is_open=True,
        )
        CourseRole.objects.create(
            course=self.course, user=self.student, role=RoleInCourse.STUDENT
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson",
            content="Test",
            order=1,
            points=100,
        )
        self.quiz = Quiz.objects.create(code="syncquiz", title="Synchronization quiz")
        self.lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.lesson, quiz=self.quiz, points=100
        )
        self.question = QuizQuestion.objects.create(
            question_type="ES", title="Essay", content="Essay"
        )
        self.assignment = QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.question, points=10
        )
        self.attempt = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("2"),
            max_score=Decimal("10"),
        )
        self.answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.question, points=2
        )
        BestQuizAttempt.update_from_attempt(self.attempt)

    def test_manual_attempt_grade_updates_best_attempt_and_course_progress(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            reverse("attempt_grade", args=[self.attempt.id]),
            {f"points_{self.answer.id}": "8"},
        )

        self.assertRedirects(response, reverse("grading_dashboard"))
        best = BestQuizAttempt.objects.get(
            user=self.student, lesson_quiz=self.lesson_quiz
        )
        progress = CourseLessonProgress.objects.get(
            user=self.student, lesson=self.lesson
        )
        self.assertEqual(best.score, Decimal("8.00"))
        self.assertEqual(best.max_score, Decimal("10.00"))
        self.assertAlmostEqual(progress.percentage, 80)

    def test_manual_grade_rejects_out_of_range_and_non_finite_points(self):
        self.client.force_login(self.teacher_user)

        response = self.client.post(
            reverse("attempt_grade", args=[self.attempt.id]),
            {f"points_{self.answer.id}": "11"},
        )
        self.assertRedirects(response, reverse("attempt_grade", args=[self.attempt.id]))
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.points, 2)

        response = self.client.post(
            reverse("answer_grade", args=[self.answer.id]),
            data=json.dumps({"points": "NaN"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.points, 2)

    def test_ajax_grade_decrease_updates_course_progress(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            reverse("answer_grade", args=[self.answer.id]),
            data=json.dumps({"points": "0.75"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score, Decimal("0.75"))
        best = BestQuizAttempt.objects.get(attempt=self.attempt)
        self.assertEqual(best.score, Decimal("0.75"))
        self.assertAlmostEqual(
            CourseLessonProgress.objects.get(
                user=self.student, lesson=self.lesson
            ).percentage,
            7.5,
        )

    def test_invalid_batch_does_not_partially_save_valid_grade(self):
        self.client.force_login(self.teacher_user)
        response = self.client.post(
            reverse("attempt_grade", args=[self.attempt.id]),
            {f"points_{self.answer.id}": "8", "points_999999999": "1"},
        )
        self.assertRedirects(response, reverse("attempt_grade", args=[self.attempt.id]))
        self.answer.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.answer.points, 2)
        self.assertEqual(self.attempt.score, 2)
        self.assertEqual(BestQuizAttempt.objects.get(attempt=self.attempt).score, 2)

    def test_student_cannot_change_grades_through_either_endpoint(self):
        self.client.force_login(self.student_user)
        response = self.client.post(
            reverse("attempt_grade", args=[self.attempt.id]),
            {f"points_{self.answer.id}": "10"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("answer_grade", args=[self.answer.id]),
            data=json.dumps({"points": "10"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.points, 2)

    def test_course_grade_uses_best_attempt_maximum_snapshot(self):
        self.assignment.points = 20
        self.assignment.save(update_fields=["points"])

        grades = calculate_user_lesson_grades(self.student, [self.lesson])

        self.assertAlmostEqual(grades[self.lesson.order], 20)

    def test_course_grade_caps_invalid_historical_quiz_score(self):
        BestQuizAttempt.objects.filter(
            user=self.student, lesson_quiz=self.lesson_quiz
        ).update(score=30, max_score=10)

        grades = calculate_user_lesson_grades(self.student, [self.lesson])

        self.assertAlmostEqual(grades[self.lesson.order], 100)

    def test_best_course_attempt_is_selected_by_percentage(self):
        lower_percentage = QuizAttempt.objects.create(
            user=self.student,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("10"),
            max_score=Decimal("100"),
        )

        BestQuizAttempt.update_from_attempt(lower_percentage)

        best = BestQuizAttempt.objects.get(
            user=self.student, lesson_quiz=self.lesson_quiz
        )
        self.assertEqual(best.attempt_id, self.attempt.id)
        self.assertEqual(best.score, Decimal("2.00"))
        self.assertEqual(best.max_score, Decimal("10.00"))

    def test_deleting_last_attempt_clears_best_attempt_and_course_grade(self):
        self.attempt.delete()

        self.assertFalse(
            BestQuizAttempt.objects.filter(
                user=self.student, lesson_quiz=self.lesson_quiz
            ).exists()
        )
        progress = CourseLessonProgress.objects.get(
            user=self.student, lesson=self.lesson
        )
        self.assertEqual(progress.percentage, 0)

    def test_direct_lock_lookup_honors_recalculation_flag_with_complete_rows(self):
        next_lesson = CourseLesson.objects.create(
            course=self.course,
            title="Next lesson",
            content="Test",
            order=2,
            points=100,
        )
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=50,
        )
        update_lesson_unlock_states(self.student, self.course)
        BestQuizAttempt.objects.filter(
            user=self.student, lesson_quiz=self.lesson_quiz
        ).update(score=0)
        CourseRole.objects.filter(course=self.course, user=self.student).update(
            needs_progress_recalculation=True
        )

        lock_status = get_lesson_lock_status(self.student, self.course)

        self.assertTrue(lock_status[next_lesson.id])
        self.assertFalse(
            CourseRole.objects.get(
                course=self.course, user=self.student
            ).needs_progress_recalculation
        )


class SubmissionResultSynchronizationTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.first()
        user = User.objects.create_user("submission_sync_student", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        self.group = ProblemGroup.objects.create(
            name="Synchronization", full_name="Synchronization"
        )
        self.problem = Problem.objects.create(
            code="subsync",
            name="Submission synchronization",
            group=self.group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        self.course = Course.objects.create(
            name="Submission course",
            slug="submission-course",
            about="Test",
            is_public=True,
            is_open=True,
        )
        CourseRole.objects.create(
            course=self.course, user=self.profile, role=RoleInCourse.STUDENT
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson",
            content="Test",
            order=1,
            points=100,
        )
        CourseLessonProblem.objects.create(
            lesson=self.lesson, problem=self.problem, score=100
        )

    def test_terminal_failure_removes_stale_best_submission_and_course_grade(self):
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="AC",
            points=10,
            case_points=100,
            case_total=100,
        )
        finished_submission(submission)
        self.assertTrue(BestSubmission.objects.filter(submission=submission).exists())

        Submission.objects.filter(id=submission.id).update(status="IE", result="IE")
        with patch(
            "judge.bridge.judge_handler.update_user_points.delay",
            side_effect=RuntimeError("broker unavailable"),
        ), patch(
            "judge.bridge.judge_handler.update_problem_stats.delay",
            side_effect=RuntimeError("broker unavailable"),
        ), patch(
            "judge.bridge.judge_handler.logger.exception"
        ) as log:
            _synchronize_terminal_failure(submission.id)
            self.assertEqual(log.call_count, 2)

        self.assertFalse(
            BestSubmission.objects.filter(
                user=self.profile, problem=self.problem
            ).exists()
        )
        progress = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson
        )
        self.assertEqual(progress.percentage, 0)

    def test_case_total_change_refreshes_course_grade(self):
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="WA",
            points=5,
            case_points=50,
            case_total=100,
        )
        finished_submission(submission)

        submission.case_total = 200
        submission.save(update_fields=["case_total"])
        finished_submission(submission)

        progress = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson
        )
        self.assertAlmostEqual(progress.percentage, 25)

    def test_course_grade_caps_invalid_historical_submission_score(self):
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="AC",
            points=10,
            case_points=300,
            case_total=100,
        )
        finished_submission(submission)

        progress = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson
        )
        self.assertAlmostEqual(progress.percentage, 100)

    @patch("judge.models.submission.cache.delete_many")
    def test_reconcile_selects_completed_fallback(self, _delete_many):
        fallback = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="WA",
            points=5,
            case_points=50,
            case_total=100,
        )
        failed = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            result="AC",
            points=10,
            case_points=100,
            case_total=100,
        )
        finished_submission(failed)
        Submission.objects.filter(id=failed.id).update(status="CE", result="CE")
        failed.status = "CE"
        failed.result = "CE"

        failed.reconcile_result_state()

        self.assertEqual(
            BestSubmission.objects.get(
                user=self.profile, problem=self.problem
            ).submission_id,
            fallback.id,
        )
