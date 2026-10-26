from datetime import timedelta
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.admin.contest import ContestAdmin
from judge.models import (
    Comment,
    Contest,
    ContestPublicRequest,
    ContestReviewRun,
    Language,
    Profile,
    PublicationRequestEvent,
)
from judge.models.notification import Notification, NotificationCategory
from judge.review.publication import publication_decision_token, publish_without_review
from judge.review.snapshots import build_contest_snapshot
from judge.review.system_bot import SYSTEM_BOT_USERNAME


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True, LANGUAGE_CODE="en")
class ContestRequestDecisionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        cls.author = User.objects.create_user("decisionauthor")
        cls.author.user_permissions.add(Permission.objects.get(codename="add_problem"))
        cls.admin_user = User.objects.create_superuser(
            "decisionadmin", "decision@example.org", "pw"
        )
        for user in (cls.author, cls.admin_user):
            Profile.objects.get_or_create(user=user, defaults={"language": language})

    def setUp(self):
        self.contest = Contest.objects.create(
            key="requestdecision",
            name="Request decision",
            is_visible=False,
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1, hours=2),
        )
        self.contest.authors.add(self.author.profile)
        snapshot = build_contest_snapshot(self.contest, [])
        self.run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            snapshot=snapshot,
            content_hash=snapshot["content_hash"],
            input_hash=snapshot["input_hash"],
        )
        self.request_row = ContestPublicRequest.objects.create(
            contest=self.contest,
            requested_by=self.author.profile,
            review_run=self.run,
            candidate_hash=self.run.content_hash,
        )
        self.url = reverse("contest_review_dashboard", args=[self.contest.key])
        self.reject_url = reverse("contest_review_reject", args=[self.contest.key])

    def test_only_superuser_sees_pending_decision_and_accept_is_guidance_not_post(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'data-review-dialog="contest-request-decision-dialog"'
        )
        self.assertContains(response, 'id="contest-request-accept-dialog"')
        self.assertContains(response, reverse("contest_edit", args=[self.contest.key]))
        self.assertNotContains(
            response,
            'action="'
            + reverse("contest_review_accept", args=[self.contest.key])
            + '"',
        )
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, "P")
        self.assertFalse(
            Notification.objects.filter(
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
            ).exists()
        )
        self.client.force_login(self.author)
        self.assertNotContains(
            self.client.get(self.url), 'id="contest-request-decision-dialog"'
        )

    def test_reject_requires_reason_and_preserves_quality_visibility_and_lock(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(
            self.client.post(self.reject_url, {"feedback": "  "}).status_code, 400
        )
        token = publication_decision_token(self.request_row)
        reason = "Too similar to an existing contest. <script>not executable</script>"
        response = self.client.post(
            self.reject_url, {"feedback": reason, "request_token": token}
        )
        self.assertEqual(response.status_code, 200)
        self.request_row.refresh_from_db()
        self.contest.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.request_row.status, "R")
        self.assertEqual(self.request_row.feedback, reason)
        self.assertFalse(self.contest.is_visible)
        self.assertFalse(self.contest.is_locked)
        self.assertEqual(self.run.readiness, "READY")
        notes = Notification.objects.filter(
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED
        )
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.get().owner_id, self.author.profile.pk)
        self.assertEqual(notes.get().extra_data["feedback"], reason)
        comment = Comment.objects.get(
            content_type=ContentType.objects.get_for_model(ContestReviewRun),
            object_id=self.run.pk,
        )
        self.assertEqual(comment.author.user.username, SYSTEM_BOT_USERNAME)
        self.assertIn("Publication request rejected", comment.body)
        self.assertIn("[user:decisionadmin]", comment.body)
        self.assertIn(reason, comment.body)
        self.assertIn("tab=discussion", notes.get().html_link)
        self.assertIn("target_comment=%s" % comment.pk, notes.get().html_link)
        self.assertIn("tab=discussion", response.json()["redirect"])
        self.client.force_login(self.author)
        response = self.client.get(self.url)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertNotIn("Too similar", soup.select_one(".review-outcome").get_text())
        discussion = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": comment.content_type_id,
                "object_id": self.run.pk,
                "target_comment": comment.pk,
                "sort_by": "time",
                "sort_order": "desc",
            },
        )
        self.assertContains(discussion, "Too similar")
        self.assertNotContains(discussion, "<script>not executable</script>")
        self.client.force_login(self.admin_user)
        self.assertEqual(
            self.client.post(
                self.reject_url, {"feedback": reason, "request_token": token}
            ).status_code,
            400,
        )
        self.assertEqual(notes.count(), 1)
        self.assertEqual(
            Comment.objects.filter(
                object_id=self.run.pk,
                content_type=ContentType.objects.get_for_model(ContestReviewRun),
            ).count(),
            1,
        )

    def test_stale_dialog_cannot_reject_replaced_request(self):
        self.client.force_login(self.admin_user)
        old_token = publication_decision_token(self.request_row)
        self.request_row.save()  # Resubmitting the same candidate changes updated_at.
        response = self.client.post(
            self.reject_url, {"feedback": "Old decision", "request_token": old_token}
        )
        self.assertEqual(response.status_code, 400)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, "P")
        self.assertFalse(
            Notification.objects.filter(
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED
            ).exists()
        )

    def test_decision_comment_uses_shared_first_run_thread(self):
        older = ContestReviewRun.objects.create(
            contest=self.contest, workflow_version=2, status="D"
        )
        ContestReviewRun.objects.filter(pk=older.pk).update(
            started_at=self.run.started_at - timedelta(days=1)
        )
        self.client.force_login(self.admin_user)
        response = self.client.post(
            self.reject_url, {"feedback": "This contest has been spoiled."}
        )
        self.assertEqual(response.status_code, 200)
        comments = Comment.objects.filter(
            content_type=ContentType.objects.get_for_model(ContestReviewRun)
        )
        self.assertEqual(comments.count(), 1)
        self.assertEqual(comments.get().object_id, older.pk)
        response = self.client.get(
            self.url, {"tab": "discussion", "target_comment": comments.get().pk}
        )
        self.assertEqual(response.context["comment_object_id"], older.pk)
        self.assertEqual(response.context["target_comment"], comments.get().pk)

    def test_regular_author_cannot_reject(self):
        self.client.force_login(self.author)
        self.assertEqual(
            self.client.post(
                self.reject_url, {"feedback": "Forged rejection"}
            ).status_code,
            403,
        )

    def test_pending_ui_replaces_request_with_withdraw_on_review_and_content(self):
        self.client.force_login(self.author)
        for url in (self.url, reverse("contest_edit", args=[self.contest.key])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            labels = [
                button.get_text(strip=True)
                for button in BeautifulSoup(response.content, "html.parser").select(
                    "button"
                )
            ]
            self.assertIn("Withdraw request", labels)
            self.assertNotIn("Request publication", labels)
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertContains(response, 'data-review-dialog="review-withdraw-dialog"')
        self.assertContains(
            response, 'data-review-dialog="contest-request-decision-dialog"'
        )
        self.client.force_login(self.author)
        response = self.client.post(
            reverse("contest_request_cancel", args=[self.contest.key])
        )
        self.assertTrue(response.json()["success"])
        self.request_row.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.request_row.status, "W")
        self.assertEqual(self.run.readiness, "READY")
        self.assertFalse(self.run.credit_consumed)
        for url in (self.url, reverse("contest_edit", args=[self.contest.key])):
            response = self.client.get(url)
            labels = [
                button.get_text(strip=True)
                for button in BeautifulSoup(response.content, "html.parser").select(
                    "button"
                )
            ]
            self.assertIn("Request publication", labels)
            self.assertNotIn("Withdraw request", labels)

    def test_publication_approves_pending_request_and_notifies_once(self):
        publish_without_review(self.contest, self.admin_user, "Explicit publication")
        self.request_row.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.request_row.status, "A")
        self.assertTrue(self.contest.is_visible)
        self.assertTrue(self.contest.is_locked)
        self.assertTrue(
            PublicationRequestEvent.objects.filter(
                contest=self.contest, status="A"
            ).exists()
        )
        notes = Notification.objects.filter(
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
        )
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.get().owner_id, self.author.profile.pk)
        comments = Comment.objects.filter(
            object_id=self.run.pk,
            content_type=ContentType.objects.get_for_model(ContestReviewRun),
        )
        self.assertEqual(comments.count(), 1)
        self.assertIn("Publication request approved", comments.get().body)
        self.assertIn("target_comment=%s" % comments.get().pk, notes.get().html_link)
        publish_without_review(self.contest, self.admin_user, "Already public")
        self.assertEqual(notes.count(), 1)
        self.assertEqual(comments.count(), 1)

    def test_admin_publication_uses_same_pending_request_and_notification(self):
        request = RequestFactory().post("/admin/")
        request.user = self.admin_user
        request.profile = self.admin_user.profile
        self.contest.is_visible = True
        self.contest.save(update_fields=["is_visible"])
        form = Mock(instance=self.contest, changed_data=["is_visible"])
        with patch("judge.admin.contest.maybe_trigger_contest_rescore"):
            ContestAdmin(Contest, admin.site).save_related(request, form, [], True)
        self.request_row.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.request_row.status, "A")
        self.assertTrue(self.contest.is_locked)
        self.assertEqual(
            Notification.objects.filter(
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
            ).count(),
            1,
        )

    def test_content_publication_approves_reviewed_request_instead_of_withdrawing(self):
        self.client.force_login(self.admin_user)
        data = {
            "key": self.contest.key,
            "name": self.contest.name,
            "authors": [self.author.profile.pk],
            "curators": [],
            "testers": [],
            "start_time": self.contest.start_time.isoformat(),
            "end_time": self.contest.end_time.isoformat(),
            "format_name": self.contest.format_name,
            "format_config": "",
            "scoreboard_visibility": self.contest.scoreboard_visibility,
            "points_precision": self.contest.points_precision,
            "description": self.contest.description,
            "is_visible": "on",
            "rows-TOTAL_FORMS": "0",
            "rows-INITIAL_FORMS": "0",
            "rows-MIN_NUM_FORMS": "0",
            "rows-MAX_NUM_FORMS": "1000",
        }
        for field in (
            "use_clarifications",
            "hide_problem_tags",
            "public_scoreboard",
            "run_pretests_only",
        ):
            if getattr(self.contest, field):
                data[field] = "on"
        response = self.client.post(
            reverse("contest_edit", args=[self.contest.key]), data
        )
        self.assertEqual(
            response.status_code,
            302,
            (
                str(response.context["form"].errors)
                + " "
                + " ".join(str(message) for message in response.context["messages"])
                if response.status_code == 200
                else ""
            ),
        )
        self.request_row.refresh_from_db()
        self.contest.refresh_from_db()
        self.assertEqual(self.request_row.status, "A")
        self.assertTrue(self.contest.is_visible)
        self.assertTrue(self.contest.is_locked)
        self.assertFalse(
            PublicationRequestEvent.objects.filter(
                contest=self.contest, status="W"
            ).exists()
        )
        self.assertEqual(
            Notification.objects.filter(
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
            ).count(),
            1,
        )

    def test_decision_control_disappears_after_rejection(self):
        self.request_row.status = "R"
        self.request_row.feedback = "Not suitable for rated publication."
        self.request_row.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'id="contest-request-decision-dialog"')
        self.assertNotIn(
            self.request_row.feedback,
            BeautifulSoup(response.content, "html.parser")
            .select_one(".review-outcome")
            .get_text(),
        )
