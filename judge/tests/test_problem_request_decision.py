from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import (
    Comment,
    Language,
    Problem,
    ProblemGroup,
    ProblemReviewRun,
    Profile,
    PublicRequest,
)
from judge.models.notification import Notification, NotificationCategory
from judge.review.publication import publication_decision_token, publish_without_review
from judge.review.snapshots import build_problem_manifest


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True, LANGUAGE_CODE="en")
class ProblemRequestDecisionTest(TestCase):
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
        cls.author = User.objects.create_user("problemdecisionauthor")
        cls.author.user_permissions.add(Permission.objects.get(codename="add_problem"))
        cls.admin = User.objects.create_superuser(
            "problemdecisionadmin", "problemdecision@example.org", "pw"
        )
        for user in (cls.author, cls.admin):
            Profile.objects.get_or_create(user=user, defaults={"language": language})
        cls.group = ProblemGroup.objects.create(
            name="problemdecision", full_name="Problem decision tests"
        )

    def setUp(self):
        self.problem = Problem.objects.create(
            code="problemdecision",
            name="Problem decision",
            group=self.group,
            time_limit=1,
            memory_limit=65536,
            points=100,
            description="Statement",
        )
        self.problem.authors.add(self.author.profile)
        snapshot = build_problem_manifest(self.problem, include_execution=True)
        self.run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            snapshot=snapshot,
            content_hash=snapshot["content_hash"],
            input_hash=snapshot["input_hash"],
        )
        self.row = PublicRequest.objects.create(
            problem=self.problem,
            requested_by=self.author.profile,
            review_run=self.run,
            candidate_hash=self.run.content_hash,
        )
        self.url = reverse("problem_review_dashboard", args=[self.problem.code])
        self.reject_url = reverse("internal_reject_problem")

    def comments(self):
        return Comment.objects.filter(
            content_type=ContentType.objects.get_for_model(ProblemReviewRun),
            object_id=self.run.pk,
        )

    def test_only_superuser_sees_pending_decision(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'data-review-dialog="problem-request-decision-dialog"'
        )
        self.assertContains(response, 'id="problem-request-accept-dialog"')
        self.assertContains(response, reverse("internal_problem_queue"))
        self.assertContains(response, reverse("problem_edit", args=[self.problem.code]))
        self.assertNotContains(
            response, 'action="' + reverse("internal_publish_problem") + '"'
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "P")
        self.assertEqual(self.comments().count(), 0)
        self.client.force_login(self.author)
        self.assertNotContains(
            self.client.get(self.url), 'id="problem-request-decision-dialog"'
        )

    def test_review_rejection_posts_one_comment_and_requester_notification(self):
        self.client.force_login(self.admin)
        payload = {
            "id": self.problem.pk,
            "feedback": "An equivalent problem is already available.",
            "request_token": publication_decision_token(self.row),
        }
        response = self.client.post(self.reject_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("tab=discussion", response.json()["redirect"])
        self.row.refresh_from_db()
        self.problem.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.row.status, "R")
        self.assertFalse(self.problem.is_public)
        self.assertFalse(self.problem.is_locked)
        self.assertEqual(self.run.readiness, "READY")
        self.assertEqual(self.comments().count(), 1)
        comment = self.comments().get()
        self.assertIn(payload["feedback"], comment.body)
        notices = Notification.objects.filter(
            category=NotificationCategory.PUBLIC_REQUEST_REJECTED
        )
        self.assertEqual(notices.count(), 1)
        self.assertEqual(notices.get().owner_id, self.author.profile.pk)
        self.assertIn("target_comment=%s" % comment.pk, notices.get().html_link)
        self.assertEqual(self.client.post(self.reject_url, payload).status_code, 409)
        self.assertEqual(self.comments().count(), 1)
        self.assertEqual(notices.count(), 1)

    def test_queue_rejection_uses_same_comment_and_notification_flow(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.reject_url,
            {"id": self.problem.pk, "feedback": "Not suitable for publication."},
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.comments().count(), 1)
        self.assertEqual(
            Notification.objects.filter(
                category=NotificationCategory.PUBLIC_REQUEST_REJECTED
            ).count(),
            1,
        )

    def test_reason_permissions_and_stale_request_guard(self):
        self.client.force_login(self.author)
        self.assertEqual(
            self.client.post(
                self.reject_url, {"id": self.problem.pk, "feedback": "Forged"}
            ).status_code,
            403,
        )
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(
                self.reject_url, {"id": self.problem.pk, "feedback": "  "}
            ).status_code,
            400,
        )
        old_token = publication_decision_token(self.row)
        self.row.save()
        self.assertEqual(
            self.client.post(
                self.reject_url,
                {
                    "id": self.problem.pk,
                    "feedback": "Old dialog",
                    "request_token": old_token,
                },
            ).status_code,
            409,
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "P")
        self.assertEqual(self.comments().count(), 0)

    def test_queue_publication_notifies_and_posts_once(self):
        self.client.force_login(self.admin)
        payload = {
            "id": self.problem.pk,
            "override_reason": "Explicit fixture publication",
        }
        response = self.client.post(reverse("internal_publish_problem"), payload)
        self.assertTrue(response.json()["success"])
        self.row.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(self.row.status, "A")
        self.assertTrue(self.problem.is_public)
        self.assertTrue(self.problem.is_locked)
        self.assertEqual(self.comments().count(), 1)
        self.assertIn("Publication request approved", self.comments().get().body)
        self.assertEqual(
            Notification.objects.filter(
                category=NotificationCategory.PUBLIC_REQUEST_APPROVED
            ).count(),
            1,
        )

    def test_shared_publication_helper_notifies_once(self):
        publish_without_review(self.problem, self.admin, "Explicit fixture publication")
        publish_without_review(self.problem, self.admin, "Already public")
        self.assertEqual(self.comments().count(), 1)
        self.assertEqual(
            Notification.objects.filter(
                category=NotificationCategory.PUBLIC_REQUEST_APPROVED
            ).count(),
            1,
        )

    def test_control_hidden_after_decision_or_withdrawal(self):
        self.client.force_login(self.admin)
        for status in ("R", "W", "A"):
            self.row.status = status
            self.row.save()
            with self.subTest(status=status):
                self.assertNotContains(
                    self.client.get(self.url), 'id="problem-request-decision-dialog"'
                )
