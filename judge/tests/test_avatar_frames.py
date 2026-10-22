"""Reward eligibility and collection authorization against real persisted scores."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from judge.jinja2.avatar import public_avatar_frame
from judge.admin.reward import AvatarFrameForm
from judge.tasks.rewards import award_course_frames
from judge.models import (
    AvatarFrame,
    BestQuizAttempt,
    BestSubmission,
    Contest,
    ContestParticipation,
    ContestProblem,
    Course,
    CourseContest,
    CourseLesson,
    CourseLessonProblem,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    ProfileFrame,
    Quiz,
    QuizAttempt,
    Submission,
)
from judge.utils.rewards import (
    get_equipped_frame,
    invalidate_equipped_frame,
    sync_course_rewards,
)


class AvatarFrameTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.group = ProblemGroup.objects.create(
            name="Reward tests", full_name="Reward tests"
        )
        cls.user = User.objects.create_user(
            username="frame_student", password="password123"
        )
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )
        cls.course = Course.objects.create(
            name="Reward course",
            slug="reward-test-course",
            about="Course",
            is_public=True,
            is_open=True,
        )
        cls.frame = AvatarFrame.objects.create(
            name="Test iron frame",
            slug="test-iron-frame",
            style="iron",
            course=cls.course,
            unlock_type="course",
        )
        cls.manual = AvatarFrame.objects.create(
            name="Test setter frame",
            slug="test-setter-frame",
            style="setter",
            label="SETTER",
            unlock_type="manual",
        )
        CourseRole.objects.create(course=cls.course, user=cls.profile)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def add_problem(self, code="rewardproblem"):
        return Problem.objects.create(
            code=code,
            name=code,
            group=self.group,
            time_limit=1,
            memory_limit=262144,
            points=100,
            is_public=True,
        )

    def add_lesson_problem(self):
        lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson",
            content="Content",
            points=100,
        )
        problem = self.add_problem()
        CourseLessonProblem.objects.create(lesson=lesson, problem=problem, score=100)
        return lesson, problem

    def submit(self, problem, score=100, total=100):
        submission = Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status="D",
            result="AC" if score == total else "WA",
            points=score,
            case_points=score,
            case_total=total,
        )
        BestSubmission.update_from_submission(submission)
        return submission

    def progress(self):
        return sync_course_rewards(self.profile)[self.frame.pk]

    def assert_locked(self):
        self.assertFalse(self.progress()["complete"])
        self.assertFalse(
            ProfileFrame.objects.filter(profile=self.profile, frame=self.frame).exists()
        )


class CourseFrameEligibilityTests(AvatarFrameTestCase):
    def test_empty_course_and_empty_weighted_lesson_do_not_unlock(self):
        self.assert_locked()
        CourseLesson.objects.create(
            course=self.course, title="Empty", content="", points=100
        )
        self.assert_locked()

    def test_partial_score_that_displays_as_100_percent_does_not_unlock(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem, score=999999, total=1000000)
        progress = self.progress()
        self.assertEqual(round(progress["percentage"], 2), 100)
        self.assert_locked()

    def test_full_completion_awards_once_and_survives_added_content(self):
        lesson, problem = self.add_lesson_problem()
        self.submit(problem)
        self.assertTrue(self.progress()["complete"])
        award = ProfileFrame.objects.get(profile=self.profile, frame=self.frame)
        self.assertFalse(award.is_equipped)
        self.progress()
        self.assertEqual(
            ProfileFrame.objects.filter(profile=self.profile, frame=self.frame).count(),
            1,
        )
        CourseLessonProblem.objects.create(
            lesson=lesson,
            problem=self.add_problem("rewardnewproblem"),
            score=100,
        )
        self.assertFalse(self.progress()["complete"])
        retained = ProfileFrame.objects.get(profile=self.profile, frame=self.frame)
        self.assertEqual(retained.pk, award.pk)
        self.assertEqual(retained.awarded_at, award.awarded_at)

    def test_full_score_requires_course_membership(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem)
        CourseRole.objects.filter(course=self.course, user=self.profile).delete()
        self.assert_locked()
        CourseRole.objects.create(course=self.course, user=self.profile)
        self.assertTrue(self.progress()["complete"])

    def test_zero_weight_content_does_not_prevent_completion(self):
        lesson, problem = self.add_lesson_problem()
        self.submit(problem)
        CourseLessonProblem.objects.create(
            lesson=lesson,
            problem=self.add_problem("optionalrewardproblem"),
            score=0,
        )
        CourseLesson.objects.create(
            course=self.course, title="Optional", content="", points=0
        )
        self.assertTrue(self.progress()["complete"])

    def test_hidden_lessons_do_not_prevent_completion(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem)
        CourseLesson.objects.create(
            course=self.course,
            title="Draft",
            content="",
            points=100,
            is_visible=False,
        )
        self.assertTrue(self.progress()["complete"])

    def test_manual_inactive_and_unmapped_frames_are_not_auto_awarded(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem)
        self.frame.is_active = False
        self.frame.save(update_fields=["is_active"])
        AvatarFrame.objects.create(name="Unmapped", slug="test-unmapped", style="gold")
        sync_course_rewards(self.profile)
        self.assertFalse(ProfileFrame.objects.filter(profile=self.profile).exists())

    def test_quiz_requires_full_visible_result(self):
        lesson, problem = self.add_lesson_problem()
        self.submit(problem)
        quiz = Quiz.objects.create(code="rewardquiz", title="Reward quiz")
        lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=lesson, quiz=quiz, points=100
        )
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=quiz,
            lesson_quiz=lesson_quiz,
            is_submitted=True,
            score=Decimal("99.99"),
            max_score=Decimal("100"),
        )
        BestQuizAttempt.update_from_attempt(attempt)
        self.assert_locked()
        BestQuizAttempt.objects.filter(
            user=self.profile, lesson_quiz=lesson_quiz
        ).update(score=100)
        lesson_quiz.is_result_hidden = True
        lesson_quiz.save(update_fields=["is_result_hidden"])
        self.assert_locked()
        lesson_quiz.is_result_hidden = False
        lesson_quiz.save(update_fields=["is_result_hidden"])
        self.assertTrue(self.progress()["complete"])

    def test_contest_requires_full_live_undisqualified_visible_result(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="rewardcontest",
            name="Reward contest",
            start_time=now - timedelta(days=2),
            end_time=now - timedelta(days=1),
            is_visible=True,
        )
        contest_problem = ContestProblem.objects.create(
            contest=contest,
            problem=self.add_problem(),
            points=100,
            order=0,
        )
        CourseContest.objects.create(course=self.course, contest=contest, points=100)
        participation = ContestParticipation.objects.create(
            contest=contest,
            user=self.profile,
            score=99,
            virtual=0,
        )
        self.assert_locked()
        ContestParticipation.objects.filter(pk=participation.pk).update(
            score=100, virtual=1
        )
        self.assert_locked()
        ContestParticipation.objects.filter(pk=participation.pk).update(
            virtual=0, is_disqualified=True
        )
        self.assert_locked()
        ContestParticipation.objects.filter(pk=participation.pk).update(
            is_disqualified=False
        )
        ContestProblem.objects.filter(pk=contest_problem.pk).update(
            is_result_hidden=True
        )
        self.assert_locked()
        ContestProblem.objects.filter(pk=contest_problem.pk).update(
            is_result_hidden=False
        )
        self.assertTrue(self.progress()["complete"])


class AvatarFrameCollectionTests(AvatarFrameTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("user_frames")
        self.client.force_login(self.user)

    def test_anonymous_get_and_post_require_login(self):
        self.client.logout()
        for response in (
            self.client.get(self.url),
            self.client.post(self.url, {"frame": ""}),
        ):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)

    def test_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, {"frame": ""}).status_code, 403)

    def test_locked_frame_cannot_be_equipped_or_awarded_by_post(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem)
        response = self.client.post(self.url, {"frame": self.frame.pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProfileFrame.objects.filter(profile=self.profile).exists())

    def test_another_users_award_does_not_authorize_equipping(self):
        user = User.objects.create_user(username="frame_other")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        ProfileFrame.objects.create(
            profile=profile, frame=self.manual, is_equipped=True
        )
        self.assertEqual(
            self.client.post(self.url, {"frame": self.manual.pk}).status_code, 403
        )
        self.assertTrue(
            ProfileFrame.objects.get(profile=profile, frame=self.manual).is_equipped
        )

    def test_switching_and_removal_leave_at_most_one_equipped_frame(self):
        first = ProfileFrame.objects.create(
            profile=self.profile, frame=self.frame, is_equipped=True
        )
        second = ProfileFrame.objects.create(profile=self.profile, frame=self.manual)
        self.assertEqual(get_equipped_frame(self.profile.pk)["slug"], self.frame.slug)
        self.assertRedirects(
            self.client.post(self.url, {"frame": self.manual.pk}),
            self.url,
            fetch_redirect_response=False,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_equipped)
        self.assertTrue(second.is_equipped)
        self.assertEqual(get_equipped_frame(self.profile.pk)["slug"], self.manual.slug)
        self.assertRedirects(
            self.client.post(self.url, {"frame": ""}),
            self.url,
            fetch_redirect_response=False,
        )
        self.assertFalse(
            ProfileFrame.objects.filter(profile=self.profile, is_equipped=True).exists()
        )
        self.assertIsNone(get_equipped_frame(self.profile.pk))

    def test_rejected_selection_keeps_current_frame(self):
        owned = ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        self.assertEqual(
            self.client.post(self.url, {"frame": self.frame.pk}).status_code, 403
        )
        owned.refresh_from_db()
        self.assertTrue(owned.is_equipped)

    def test_owned_inactive_frame_cannot_be_equipped(self):
        ProfileFrame.objects.create(profile=self.profile, frame=self.manual)
        self.manual.is_active = False
        self.manual.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.post(self.url, {"frame": self.manual.pk}).status_code, 403
        )

    def test_malformed_selection_is_bad_request(self):
        for payload in (
            {},
            {"frame": "invalid"},
            {"frame": "-1"},
            {"frame": "2147483648"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.url, payload).status_code, 400)

    def test_collection_get_awards_completed_course_and_lists_locked_manual_frame(self):
        _, problem = self.add_lesson_problem()
        self.submit(problem)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.frame.name)
        self.assertContains(response, self.manual.name)
        self.assertTrue(
            ProfileFrame.objects.filter(profile=self.profile, frame=self.frame).exists()
        )
        self.assertFalse(
            ProfileFrame.objects.filter(
                profile=self.profile, frame=self.manual
            ).exists()
        )


class AvatarFramePresentationTests(AvatarFrameTestCase):
    def test_uploaded_frame_replaces_css_art_and_preserves_alignment(self):
        self.manual.artwork = "avatar_frames/custom-setter.png"
        self.manual.avatar_center_x = Decimal("53.50")
        self.manual.avatar_center_y = Decimal("55.80")
        self.manual.avatar_diameter = Decimal("41.00")
        self.manual.save()
        ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        frame = get_equipped_frame(self.profile.pk)
        self.assertEqual(frame["artwork_url"], self.manual.artwork.url)
        self.assertAlmostEqual(frame["artwork_scale"], 100 / 41, places=5)
        self.client.force_login(self.user)
        response = self.client.get(reverse("user_frames"))
        self.assertContains(response, "avatar-crest__artwork")
        self.assertContains(response, "custom-setter.png")
        self.assertContains(response, "--frame-x: 53.5%")
        self.assertContains(response, "--frame-y: 55.8%")

    def test_removing_uploaded_frame_returns_to_builtin_style(self):
        self.manual.artwork = "avatar_frames/custom-setter.png"
        self.manual.save()
        ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        self.assertTrue(get_equipped_frame(self.profile.pk)["artwork_url"])
        self.manual.artwork = ""
        self.manual.save()
        self.assertEqual(get_equipped_frame(self.profile.pk)["artwork_url"], "")

    def test_invalid_avatar_alignment_is_rejected(self):
        for field, value in (
            ("avatar_diameter", 0),
            ("avatar_center_x", -1),
            ("avatar_center_y", 101),
        ):
            with self.subTest(field=field):
                frame = AvatarFrame(name="Invalid", slug="invalid", style="iron")
                setattr(frame, field, value)
                with self.assertRaises(ValidationError):
                    frame.full_clean()

    def test_equipped_avatar_renders_in_collection_and_leaderboard(self):
        ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        self.client.force_login(self.user)
        for url in (reverse("user_frames"), reverse("user_list")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "avatar-crest--setter")

    def test_invalidation_runs_now_and_after_commit(self):
        with patch.object(get_equipped_frame, "dirty") as dirty:
            with self.captureOnCommitCallbacks(execute=True):
                invalidate_equipped_frame(self.profile)
                dirty.assert_called_once_with(self.profile.pk)
            self.assertEqual(dirty.call_count, 2)

    def test_award_updates_and_catalog_changes_invalidate_cached_presentation(self):
        with patch("judge.signals.reward.invalidate_equipped_frame") as invalidate:
            award = ProfileFrame.objects.create(
                profile=self.profile, frame=self.manual, is_equipped=True
            )
            invalidate.assert_called_with(self.profile.pk)
            invalidate.reset_mock()
            self.manual.name = "Renamed setter"
            self.manual.save(update_fields=["name"])
            invalidate.assert_called_with(self.profile.pk)
            invalidate.reset_mock()
            award.delete()
            invalidate.assert_called_with(self.profile.pk)

    def test_deactivated_frame_is_not_publicly_displayed(self):
        ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        self.assertEqual(get_equipped_frame(self.profile.pk)["slug"], self.manual.slug)
        self.manual.is_active = False
        self.manual.save(update_fields=["is_active"])
        self.assertIsNone(get_equipped_frame(self.profile.pk))

    def test_hidden_identity_never_fetches_or_displays_frame(self):
        viewer = AnonymousUser()
        with patch.object(
            Profile, "should_hide_public_identity", return_value=True
        ) as hidden:
            with patch("judge.jinja2.avatar.get_equipped_frame") as lookup:
                self.assertIsNone(public_avatar_frame(self.profile, viewer))
                hidden.assert_called_once_with(viewer)
                lookup.assert_not_called()

    def test_visible_identity_displays_equipped_frame(self):
        ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, is_equipped=True
        )
        frame = public_avatar_frame(self.profile, AnonymousUser())
        self.assertEqual(frame["slug"], self.manual.slug)
        self.assertEqual(frame["label"], "SETTER")


class CourseRewardPeriodicTests(SimpleTestCase):
    @override_settings(COURSE_REWARDS_ENABLED=False)
    @patch("judge.tasks.rewards.run_locked_command")
    def test_disabled_task_does_not_scan_courses(self, run):
        self.assertEqual(award_course_frames(), {"skipped": True, "reason": "disabled"})
        run.assert_not_called()

    @override_settings(COURSE_REWARDS_ENABLED=True, COURSE_REWARDS_BATCH_SIZE=25)
    @patch("judge.tasks.rewards.run_locked_command")
    def test_periodic_sync_uses_lock_and_configured_batch(self, run):
        award_course_frames()
        run.assert_called_once_with(
            "periodic:award_course_frames",
            "sync_course_rewards",
            "--batch-size",
            "25",
            lock_timeout=3600,
        )


class AvatarFrameArtworkUploadTests(SimpleTestCase):
    def image_upload(self, transparent, invisible=False):
        # Synthetic pixels exercise upload validation, not production artwork.
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0 if transparent else 255))
        if transparent and not invisible:
            image.putpixel((0, 0), (128, 128, 128, 255))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return SimpleUploadedFile(
            "frame.png", buffer.getvalue(), content_type="image/png"
        )

    def test_transparent_artwork_is_accepted_without_changing_bytes(self):
        upload = self.image_upload(True)
        expected = upload.read()
        upload.seek(0)
        form = AvatarFrameForm()
        form.cleaned_data = {"artwork": upload}
        self.assertEqual(form.clean_artwork().read(), expected)

    def test_opaque_and_invisible_artwork_are_rejected(self):
        for upload in (
            self.image_upload(False),
            self.image_upload(True, invisible=True),
        ):
            form = AvatarFrameForm()
            form.cleaned_data = {"artwork": upload}
            with self.assertRaises(ValidationError):
                form.clean_artwork()
