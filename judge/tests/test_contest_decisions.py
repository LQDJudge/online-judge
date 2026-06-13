from datetime import timedelta

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from judge.admin.contest import ContestPublicRequestAdmin
from judge.models import Contest, Language, Profile
from judge.models.contest_review import ContestPublicRequest, ContestReviewRun
from judge.models.notification import Notification, NotificationCategory
from judge.review.contest_hashing import compute_contest_input_hash
from judge.review.decisions import (
    accept_contest_public_request,
    reject_contest_public_request,
)


def _make_language():
    lang, _ = Language.objects.get_or_create(
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
    return lang


@override_settings(LANGUAGE_CODE="en")
class ContestDecisionServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        author_user = User.objects.create_user("author", "a@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=author_user, defaults={"language": cls.language}
        )
        admin_user = User.objects.create_superuser("admin", "ad@x.com", "pw")
        cls.admin, _ = Profile.objects.get_or_create(
            user=admin_user, defaults={"language": cls.language}
        )

    def setUp(self):
        now = timezone.now()
        self.contest = Contest.objects.create(
            key="dc1",
            name="Decision Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        self.contest.authors.add(self.author)
        # A review run is required so the system comment has an anchor.
        ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author,
            input_hash=compute_contest_input_hash(self.contest),
        )
        self.pr = ContestPublicRequest.objects.create(
            contest=self.contest, requested_by=self.author
        )

    def test_accept_sets_status_and_does_not_publish(self):
        accept_contest_public_request(self.contest, self.admin, feedback="lgtm")
        self.pr.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.APPROVED)
        self.assertEqual(self.pr.feedback, "lgtm")
        self.assertEqual(self.pr.reviewed_by_id, self.admin.id)
        # The whole point: accept must NOT publish.
        self.assertFalse(self.contest.is_visible)

    def test_reject_sets_status_and_does_not_publish(self):
        reject_contest_public_request(self.contest, self.admin, feedback="needs work")
        self.pr.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.REJECTED)
        self.assertEqual(self.pr.feedback, "needs work")
        self.assertFalse(self.contest.is_visible)

    def test_accept_notifies_author(self):
        accept_contest_public_request(self.contest, self.admin)
        self.assertTrue(
            Notification.objects.filter(
                owner=self.author,
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED,
            ).exists()
        )

    def test_reject_notifies_author(self):
        reject_contest_public_request(self.contest, self.admin)
        self.assertTrue(
            Notification.objects.filter(
                owner=self.author,
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED,
            ).exists()
        )

    def test_no_request_is_safe_noop(self):
        self.pr.delete()
        result = accept_contest_public_request(self.contest, self.admin)
        self.assertIsNone(result)

    def test_accept_posts_system_comment(self):
        from judge.models import Comment

        accept_contest_public_request(self.contest, self.admin, feedback="lgtm")
        self.assertTrue(
            Comment.objects.filter(body__icontains="Review accepted").exists()
        )

    def test_repeat_same_status_does_not_duplicate_comment_or_notification(self):
        from judge.models import Comment

        accept_contest_public_request(self.contest, self.admin, feedback="lgtm")
        # Second accept with the same verdict (e.g. accidental double-click or
        # a feedback-only edit) must not post another comment or notify again.
        accept_contest_public_request(self.contest, self.admin, feedback="lgtm again")
        self.assertEqual(
            Comment.objects.filter(body__icontains="Review accepted").count(), 1
        )
        self.assertEqual(
            Notification.objects.filter(
                owner=self.author,
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED,
            ).count(),
            1,
        )
        # But the feedback edit was still saved.
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.feedback, "lgtm again")


@override_settings(LANGUAGE_CODE="en")
class ContestAdminDecisionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        adu = User.objects.create_superuser("aadmin", "aad@x.com", "pw")
        cls.admin, _ = Profile.objects.get_or_create(
            user=adu, defaults={"language": cls.language}
        )
        au = User.objects.create_user("aauthor", "aau@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=au, defaults={"language": cls.language}
        )

    def setUp(self):
        now = timezone.now()
        self.contest = Contest.objects.create(
            key="adc",
            name="Admin Decision Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        self.contest.authors.add(self.author)
        ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author,
            input_hash=compute_contest_input_hash(self.contest),
        )
        self.pr = ContestPublicRequest.objects.create(
            contest=self.contest, requested_by=self.author
        )

    def test_admin_approve_does_not_publish(self):
        rf = RequestFactory()
        request = rf.post("/admin/judge/contestpublicrequest/%d/change/" % self.pr.id)
        request.user = self.admin.user

        model_admin = ContestPublicRequestAdmin(ContestPublicRequest, django_admin.site)
        # Simulate the admin form having set status -> APPROVED on the instance.
        self.pr.status = ContestPublicRequest.APPROVED
        self.pr.feedback = "ok"
        model_admin.save_model(request, self.pr, form=None, change=True)

        self.pr.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.APPROVED)
        # The behavior change: admin approve no longer publishes.
        self.assertFalse(self.contest.is_visible)

    def test_admin_reject_sets_status(self):
        rf = RequestFactory()
        request = rf.post("/admin/judge/contestpublicrequest/%d/change/" % self.pr.id)
        request.user = self.admin.user

        model_admin = ContestPublicRequestAdmin(ContestPublicRequest, django_admin.site)
        self.pr.status = ContestPublicRequest.REJECTED
        self.pr.feedback = "no"
        model_admin.save_model(request, self.pr, form=None, change=True)

        self.pr.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.REJECTED)
        self.assertFalse(self.contest.is_visible)
