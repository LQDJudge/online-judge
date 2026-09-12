import json
import tempfile
import threading
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.fields.files import FieldFile
from django.db import close_old_connections, transaction
from django.http import QueryDict
from django.test import (
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone

from judge.management.commands.expire_quiz_attempts import CURSOR_KEY
from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    Course,
    CourseLesson,
    CourseLessonQuiz,
    CourseLessonProgress,
    CourseRole,
    Language,
    Profile,
    Quiz,
    QuizAnswer,
    QuizAnswerFile,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionAssignment,
)
from judge.tasks.quiz import expire_quiz_attempts
from judge.utils.quiz_attempts import finalize_locked_attempt
from judge.views.quiz import QuizSaveAnswer, QuizSubmit, LessonQuizSubmit


class QuizDeadlineTests(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user("deadline-student", password="pw")
        self.profile, unused = Profile.objects.get_or_create(
            user=self.user, defaults={"language": Language.objects.first()}
        )
        self.quiz = Quiz.objects.create(
            code="deadline-quiz", title="Deadline", time_limit=20
        )
        self.question = QuizQuestion.objects.create(
            question_type="MC",
            title="Question",
            content="Test",
            choices=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            correct_answers={"answers": "a"},
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.question, points=10
        )
        self.attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, time_limit_minutes=20
        )
        self.deadline = self.attempt.deadline_at
        self.answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.question, answer="a"
        )
        self.answered_at = self.answer.answered_at
        cache.delete(CURSOR_KEY)
        self.addCleanup(cache.delete, CURSOR_KEY)

    def submit(self, at, value="b", lesson=False):
        request = RequestFactory().post("/", {f"q_{self.question.id}": value})
        request.user = self.user
        request.profile = self.profile
        request.session = {}
        request._messages = FallbackStorage(request)
        view = LessonQuizSubmit() if lesson else QuizSubmit()
        if lesson:
            view.validate_attempt_context = lambda attempt: None
            view.get_lesson_url_kwargs = lambda **kwargs: {
                "course_slug": "course",
                "lesson_id": 1,
                "code": self.quiz.code,
                **kwargs,
            }
        with patch("judge.views.quiz.redirect"), patch(
            "django.utils.timezone.now", return_value=at
        ):
            view.post(request, code=self.quiz.code, attempt_id=self.attempt.id)
        self.attempt.refresh_from_db()
        self.answer.refresh_from_db()

    def sweep(self, at, **options):
        output = StringIO()
        with patch("django.utils.timezone.now", return_value=at):
            call_command("expire_quiz_attempts", stdout=output, **options)
        return json.loads(output.getvalue())

    def test_manual_before_deadline_accepts_latest_form_and_time(self):
        now = self.deadline - timedelta(seconds=1)
        self.submit(now)
        self.assertEqual(self.answer.answer, "b")
        self.assertEqual(self.attempt.effective_end_time, now)
        self.assertEqual(self.attempt.end_time, now)
        self.assertEqual(self.attempt.score, 0)

    def test_exact_deadline_rejects_new_answers(self):
        self.submit(self.deadline)
        self.assertEqual(self.answer.answer, "a")
        self.assertEqual(self.answer.answered_at, self.answered_at)
        self.assertEqual(self.attempt.score, 10)
        self.assertEqual(self.attempt.effective_end_time, self.deadline)

    def test_new_submitted_answer_retains_captured_time_during_slow_insert(self):
        self.answer.delete()
        captured = self.deadline - timedelta(seconds=1)
        field = QuizAnswer._meta.get_field("answered_at")
        with transaction.atomic(), patch.object(
            field, "pre_save", return_value=self.deadline + timedelta(seconds=1)
        ):
            attempt = QuizAttempt.objects.select_for_update().get(pk=self.attempt.pk)
            finalize_locked_attempt(
                attempt,
                QueryDict(f"q_{self.question.pk}=a"),
                now=captured,
            )
        answer = self.attempt.answers.get()
        self.assertEqual(answer.answered_at, captured)
        self.assertEqual(answer.answer, "a")

    def test_autosave_retains_decision_time_during_slow_save(self):
        captured = self.deadline - timedelta(seconds=1)
        field = QuizAnswer._meta.get_field("answered_at")
        for existing in (True, False):
            with self.subTest(existing=existing):
                if not existing:
                    self.attempt.answers.all().delete()
                request = RequestFactory().post(
                    "/",
                    json.dumps({"question_id": self.question.pk, "answer": "b"}),
                    content_type="application/json",
                )
                request.profile = self.profile
                with patch(
                    "django.utils.timezone.now", return_value=captured
                ), patch.object(
                    field, "pre_save", return_value=self.deadline + timedelta(seconds=1)
                ):
                    response = QuizSaveAnswer().post(
                        request, attempt_id=self.attempt.pk
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.attempt.answers.get().answered_at, captured)
                self.assertEqual(
                    json.loads(response.content)["saved_at"], captured.isoformat()
                )

    def test_late_lesson_submit_preserves_saved_answer(self):
        self.submit(self.deadline + timedelta(days=10), lesson=True)
        self.assertEqual(self.answer.answer, "a")
        self.assertEqual(self.attempt.score, 10)
        self.assertEqual(self.attempt.effective_end_time, self.deadline)

    def test_closed_browser_sweep_is_idempotent(self):
        now = self.deadline + timedelta(days=10)
        result = self.sweep(now)
        self.assertEqual(result["finalized"], 1)
        self.attempt.refresh_from_db()
        self.answer.refresh_from_db()
        self.assertEqual(self.attempt.end_time, now)
        self.assertEqual(self.attempt.effective_end_time, self.deadline)
        self.assertEqual(self.answer.answered_at, self.answered_at)
        self.assertEqual(self.attempt.score, 10)
        self.assertEqual(self.sweep(now + timedelta(seconds=30))["finalized"], 0)
        self.submit(now + timedelta(days=1))
        self.assertEqual(self.attempt.end_time, now)

    def test_dry_run_and_legacy_attempts_are_not_finalized(self):
        self.assertEqual(
            self.sweep(self.deadline, dry_run=True)["candidate_ids"], [self.attempt.id]
        )
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.is_submitted)
        QuizAttempt.objects.filter(pk=self.attempt.id).update(
            deadline_initialized=False, deadline_at=None
        )
        self.assertEqual(self.sweep(self.deadline)["finalized"], 0)
        self.submit(self.deadline)
        self.assertEqual(self.answer.answer, "a")

    def test_untimed_snapshot_remains_untimed(self):
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, time_limit_minutes=0
        )
        self.quiz.time_limit = 1
        self.quiz.save()
        self.assertTrue(attempt.deadline_initialized)
        self.assertIsNone(attempt.get_deadline())
        self.assertFalse(attempt.is_expired())

    def test_settings_edits_do_not_change_existing_deadline(self):
        self.attempt.time_limit_minutes = 1
        self.attempt.save()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.get_deadline(), self.deadline)

    def test_contest_deadline_exact_seconds_and_time_scoring(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="deadlinecontest",
            name="Deadline",
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
            time_limit=timedelta(seconds=45),
            format_name="default",
        )
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.profile, virtual=1, real_start=now
        )
        cp = ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=1
        )
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            contest_participation=participation,
            time_limit_minutes=20,
        )
        self.assertEqual(attempt.deadline_at, now + timedelta(seconds=45))
        QuizAnswer.objects.create(attempt=attempt, question=self.question, answer="a")
        contest.time_limit = timedelta(hours=2)
        contest.save()
        attempt.refresh_from_db()
        self.assertEqual(attempt.get_deadline(), now + timedelta(seconds=45))
        with patch("django.utils.timezone.now", return_value=now + timedelta(days=10)):
            attempt.auto_submit()
        participation.refresh_from_db()
        self.assertEqual(participation.score, 100)
        self.assertEqual(participation.format_data[f"quiz_{cp.id}"]["time"], 45)

    def test_failure_rolls_back_and_does_not_starve_next_batch(self):
        second = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, time_limit_minutes=1
        )
        QuizAttempt.objects.filter(pk=second.pk).update(deadline_at=self.deadline)
        with patch(
            "judge.management.commands.expire_quiz_attempts.finalize_locked_attempt",
            side_effect=RuntimeError("grading failed"),
        ), self.assertLogs(
            "judge.management.commands.expire_quiz_attempts", level="ERROR"
        ):
            self.assertEqual(self.sweep(self.deadline, batch_size=1)["failed"], 1)
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.is_submitted)
        self.assertEqual(self.sweep(self.deadline, batch_size=1)["finalized"], 1)
        second.refresh_from_db()
        self.assertTrue(second.is_submitted)
        self.assertEqual(self.sweep(self.deadline, batch_size=1)["finalized"], 1)

    def test_grading_failure_rolls_back_submission_and_answers(self):
        with patch(
            "judge.utils.quiz_attempts.auto_grade_quiz_attempt",
            side_effect=RuntimeError("failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit(self.deadline - timedelta(seconds=1))
        self.attempt.refresh_from_db()
        self.answer.refresh_from_db()
        self.assertFalse(self.attempt.is_submitted)
        self.assertIsNone(self.attempt.end_time)
        self.assertEqual(self.answer.answer, "a")

    def test_status_owner_only_and_read_only(self):
        url = reverse("quiz_attempt_status", args=[self.quiz.code, self.attempt.id])
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 200)
        other = User.objects.create_user("deadline-other", password="pw")
        Profile.objects.create(user=other, language=Language.objects.first())
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_late_autosave_rejected(self):
        self.client.force_login(self.user)
        url = reverse("quiz_save_answer", args=[self.quiz.code, self.attempt.id])
        with patch("django.utils.timezone.now", return_value=self.deadline):
            response = self.client.post(
                url,
                json.dumps({"question_id": self.question.id, "answer": "b"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["expired"])
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.answer, "a")

    def test_upload_finishing_after_deadline_is_removed_without_attachment(self):
        self.question.question_type = "ES"
        self.question.save()
        self.client.force_login(self.user)
        current = [self.deadline - timedelta(seconds=1)]
        original_save = FieldFile.save

        def slow_storage(field, *args, **kwargs):
            result = original_save(field, *args, **kwargs)
            current[0] = self.deadline
            return result

        with tempfile.TemporaryDirectory() as directory, override_settings(
            MEDIA_ROOT=directory
        ):
            with patch(
                "django.utils.timezone.now", side_effect=lambda: current[0]
            ), patch.object(FieldFile, "save", slow_storage):
                response = self.client.post(
                    reverse("quiz_upload_file", args=[self.quiz.code, self.attempt.id]),
                    {
                        "question_id": self.question.id,
                        "file": SimpleUploadedFile("essay.txt", b"essay"),
                    },
                )
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["expired"])
            self.assertFalse(QuizAnswerFile.objects.filter(answer=self.answer).exists())
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.is_submitted)

    def test_expired_attachment_delete_preserves_saved_file(self):
        self.client.force_login(self.user)
        file = QuizAnswerFile.objects.create(
            answer=self.answer,
            file="not-a-real-file.txt",
            original_filename="essay.txt",
        )
        with patch("django.utils.timezone.now", return_value=self.deadline):
            response = self.client.post(reverse("quiz_delete_file", args=[file.id]))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(QuizAnswerFile.objects.filter(pk=file.pk).exists())

    def test_course_progress_updated_by_unattended_expiry(self):
        course = Course.objects.create(
            name="Deadline course", slug="deadline-course", about="Test", is_public=True
        )
        CourseRole.objects.create(course=course, user=self.profile, role="ST")
        lesson = CourseLesson.objects.create(
            course=course, title="Lesson", content="Test", order=1, points=100
        )
        link = CourseLessonQuiz.objects.create(
            lesson=lesson, quiz=self.quiz, points=100
        )
        QuizAttempt.objects.filter(pk=self.attempt.id).update(lesson_quiz=link)
        self.assertEqual(self.sweep(self.deadline)["finalized"], 1)
        self.assertEqual(
            CourseLessonProgress.objects.get(
                user=self.profile, lesson=lesson
            ).percentage,
            100,
        )

    def test_notification_failure_cannot_undo_finalization(self):
        with patch(
            "judge.utils.quiz_attempts.notify_graders_for_essay",
            side_effect=RuntimeError("offline"),
        ) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.submit(self.deadline)
            notify.assert_called_once()
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.is_submitted)

    def test_expiry_preserves_manual_grades(self):
        self.answer.points = 7
        self.answer.graded_by = self.profile
        self.answer.graded_at = self.answered_at
        self.answer.save(update_fields=["points", "graded_by", "graded_at"])
        self.submit(self.deadline)
        self.assertEqual(self.attempt.score, 7)
        self.assertEqual(self.answer.graded_at, self.answered_at)

    def test_manual_grade_does_not_change_answer_timestamp(self):
        self.quiz.authors.add(self.profile)
        self.submit(self.deadline)
        self.client.force_login(self.user)
        with patch(
            "django.utils.timezone.now", return_value=self.deadline + timedelta(days=1)
        ):
            response = self.client.post(
                reverse("answer_grade", args=[self.answer.id]),
                json.dumps({"points": 7}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.answered_at, self.answered_at)

    @override_settings(QUIZ_EXPIRY_ENABLED=False)
    def test_task_disabled_by_default(self):
        with patch("judge.tasks.quiz.run_locked_command") as run:
            self.assertEqual(expire_quiz_attempts()["reason"], "disabled")
            run.assert_not_called()

    @override_settings(QUIZ_EXPIRY_ENABLED=True, QUIZ_EXPIRY_BATCH_SIZE=7)
    def test_task_uses_bounded_locked_command(self):
        with patch("judge.tasks.quiz.run_locked_command") as run:
            expire_quiz_attempts()
            self.assertIn("7", run.call_args.args)


class QuizDeadlineConcurrencyTests(TransactionTestCase):
    fixtures = ["language_small"]
    setUp = QuizDeadlineTests.setUp
    sweep = QuizDeadlineTests.sweep

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_worker_skips_locked_attempt_and_duplicate_finalization_is_noop(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        finished_at = self.deadline + timedelta(seconds=1)

        def submit_in_other_connection():
            close_old_connections()
            try:
                with transaction.atomic():
                    attempt = QuizAttempt.objects.select_for_update().get(
                        pk=self.attempt.pk
                    )
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("test lock release timed out")
                    finalize_locked_attempt(attempt, now=finished_at)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=submit_in_other_connection)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(self.sweep(finished_at)["skipped"], 1)
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with transaction.atomic():
            attempt = QuizAttempt.objects.select_for_update().get(pk=self.attempt.pk)
            with patch("judge.utils.quiz_attempts.auto_grade_quiz_attempt") as grade:
                finalize_locked_attempt(
                    attempt, QueryDict("q_1=b"), now=finished_at + timedelta(days=1)
                )
                grade.assert_not_called()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.end_time, finished_at)
        self.assertEqual(self.attempt.score, 10)
