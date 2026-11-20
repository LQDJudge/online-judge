from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.http import Http404
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from chat_box.models import ChatModerationLog, Message, Room
from judge.models import (
    AIUsageLog,
    BlogPost,
    Comment,
    CommentModerationLog,
    Language,
    Problem,
    Profile,
    Solution,
)
from judge.moderation_status import MuseStatus
from judge.tasks.omni_moderation import (
    content_hash,
    enqueue_omni,
    moderate_omni_item,
    public_comment_filter,
    record_omni_pending,
    recover_omni_items,
)
from judge.views.comment.edit_views import CommentEditAjax
from llm_service.omni_moderation import OmniModerationError


class OmniModerationTaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3", defaults={"name": "Python 3", "common_name": "Python"}
        )

    def setUp(self):
        self.user = User.objects.create_user(username="omni_test")
        self.profile = Profile.objects.create(user=self.user, language=self.language)
        self.room = Room.objects.get(singleton_key="lobby")
        self.message = Message.objects.create(
            room=self.room, author=self.profile, body="hello"
        )
        self.blog = BlogPost.objects.create(
            title="test",
            slug="omni-test",
            content="test",
            publish_on=timezone.now(),
            visible=True,
        )
        self.comment = Comment.objects.create(
            author=self.profile,
            body="hello",
            content_type=ContentType.objects.get_for_model(BlogPost),
            object_id=self.blog.pk,
        )

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_flagged_content_creates_review_in_existing_logs(self, call):
        call.return_value = {"flagged": True, "categories": {"harassment": True}}
        for kind, obj in (("chat", self.message), ("comment", self.comment)):
            self.assertEqual(
                moderate_omni_item(kind, obj.pk, content_hash(obj.body)),
                {"flagged": True},
            )
        self.assertEqual(
            ChatModerationLog.objects.get(message=self.message).action, "review"
        )
        self.assertEqual(
            CommentModerationLog.objects.get(comment=self.comment).action, "review"
        )
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_score_above_routing_threshold_goes_to_muse(self, call):
        call.return_value = {
            "flagged": False,
            "categories": {"harassment": False},
            "category_scores": {"harassment": 0.10},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        log = ChatModerationLog.objects.get(message=self.message)
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_score_below_routing_threshold_stays_visible(self, call):
        call.return_value = {
            "flagged": False,
            "categories": {"harassment": False},
            "category_scores": {"harassment": 0.099},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        log = ChatModerationLog.objects.get(message=self.message)
        self.assertEqual(log.muse_status, MuseStatus.NONE)
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_075_flagged_severe_category_hides_and_queues(self, call):
        call.return_value = {
            "flagged": True,
            "categories": {"violence/graphic": True},
            "category_scores": {"violence/graphic": 0.75},
            "category_applied_input_types": {"violence/graphic": ["text"]},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        self.message.refresh_from_db()
        self.assertTrue(self.message.hidden)
        self.assertEqual(
            ChatModerationLog.objects.get(message=self.message).muse_status,
            MuseStatus.PENDING_HIDDEN,
        )

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_below_075_flagged_severe_category_stays_visible(self, call):
        call.return_value = {
            "flagged": True,
            "categories": {"violence/graphic": True},
            "category_scores": {"violence/graphic": 0.749},
            "category_applied_input_types": {"violence/graphic": ["text"]},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)
        self.assertEqual(
            ChatModerationLog.objects.get(message=self.message).muse_status,
            MuseStatus.PENDING_VISIBLE,
        )

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_high_severity_needs_supported_category_to_hide(self, call):
        call.return_value = {
            "flagged": True,
            "categories": {"violence/graphic": True},
            "category_scores": {"violence/graphic": 0.995},
            "category_applied_input_types": {"violence/graphic": []},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        log = ChatModerationLog.objects.get(message=self.message)
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_usage_log_retains_valid_scores_for_calibration(self, call):
        call.return_value = {
            "flagged": True,
            "categories": {"harassment": True},
            "category_scores": {"harassment": 0.72, "invalid": float("nan")},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        log = AIUsageLog.objects.filter(feature="chat_moderation").latest("pk")
        self.assertEqual(log.metadata["category_scores"], {"harassment": 0.72})

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_pending_results_do_not_appear_in_internal_moderation_pages(self, call):
        call.return_value = {"flagged": True, "categories": {"harassment": True}}
        for kind, obj in (("chat", self.message), ("comment", self.comment)):
            moderate_omni_item(kind, obj.pk, content_hash(obj.body))
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.user)
        for name in ("internal_chat_moderation", "internal_comment_moderation"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "OpenAI")

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_duplicate_and_human_decisions_are_respected(self, call):
        ChatModerationLog.log_action(
            message=self.message, action="keep", is_automated=False
        )
        self.assertEqual(
            moderate_omni_item(
                "chat", self.message.pk, content_hash(self.message.body)
            )["skipped"],
            "already reviewed",
        )
        call.assert_not_called()

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_edit_during_call_discards_stale_result(self, call):
        def edit(*args):
            Comment.objects.filter(pk=self.comment.pk).update(body="new text")
            return {"flagged": True, "categories": {"harassment": True}}

        call.side_effect = edit
        self.assertEqual(
            moderate_omni_item("comment", self.comment.pk, content_hash("hello"))[
                "skipped"
            ],
            "changed",
        )
        self.assertFalse(
            CommentModerationLog.objects.filter(comment=self.comment).exists()
        )

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_private_content_never_reaches_api(self, call):
        self.blog.visible = False
        self.blog.save(update_fields=["visible"])
        self.assertEqual(
            moderate_omni_item("comment", self.comment.pk, content_hash("hello"))[
                "skipped"
            ],
            "stale or private",
        )
        call.assert_not_called()

    def test_edit_cannot_restore_a_comment_hidden_after_form_load(self):
        form = Mock(instance=self.comment)
        Comment.objects.filter(pk=self.comment.pk).update(hidden=True)
        with self.assertRaises(Http404):
            CommentEditAjax().form_valid(form)
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.hidden)

    def test_edit_supersedes_pending_muse_result(self):
        old_log = CommentModerationLog.log_action(
            comment=self.comment,
            action="review",
            is_automated=True,
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        self.comment.body = "edited text"
        self.comment.save(update_fields=["body"])
        self.assertTrue(record_omni_pending("comment", self.comment))
        old_log.refresh_from_db()
        self.assertEqual(old_log.muse_status, MuseStatus.SUPERSEDED)
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.user)
        response = self.client.get(reverse("internal_comment_moderation"))
        self.assertNotContains(response, "edited text")

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_editorial_comments_require_a_public_problem(self, call):
        problem = Problem.objects.create(
            code="omniprivate",
            name="Private",
            time_limit=1,
            memory_limit=65536,
            points=1,
            is_public=False,
        )
        solution = Solution.objects.create(
            problem=problem, is_public=True, publish_on=timezone.now(), content="test"
        )
        self.comment.content_type = ContentType.objects.get_for_model(Solution)
        self.comment.object_id = solution.pk
        self.comment.save(update_fields=["content_type", "object_id"])
        for public, org_private, expected in (
            (False, False, False),
            (True, True, False),
            (True, False, True),
        ):
            Problem.objects.filter(pk=problem.pk).update(
                is_public=public, is_organization_private=org_private
            )
            self.comment.refresh_from_db()
            self.assertEqual(
                Comment.objects.filter(pk=self.comment.pk)
                .filter(public_comment_filter())
                .exists(),
                expected,
            )
            if not expected:
                self.assertEqual(
                    moderate_omni_item(
                        "comment", self.comment.pk, content_hash(self.comment.body)
                    )["skipped"],
                    "stale or private",
                )
        call.assert_not_called()

    @patch("judge.tasks.omni_moderation.moderate_omni_item.apply_async")
    @patch.object(moderate_omni_item.app, "connection_for_write")
    def test_enqueue_disables_publish_retries(self, connection_for_write, publish):
        enqueue_omni("chat", self.message.pk, self.message.body)
        connection_for_write.return_value.__enter__.return_value.ensure_connection.assert_called_once_with(
            max_retries=0
        )
        self.assertFalse(publish.call_args.kwargs["retry"])
        self.assertEqual(
            publish.call_args.kwargs["args"],
            ("chat", self.message.pk, content_hash(self.message.body)),
        )

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_non_lobby_chat_never_reaches_api(self, call):
        room = Room.objects.create(room_type=Room.Type.GROUP, name="private group")
        message = Message.objects.create(room=room, author=self.profile, body="private")
        result = moderate_omni_item("chat", message.pk, content_hash(message.body))
        self.assertEqual(result["skipped"], "stale or private")
        call.assert_not_called()

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_private_organization_post_never_reaches_api(self, call):
        self.blog.is_organization_private = True
        self.blog.save(update_fields=["is_organization_private"])
        result = moderate_omni_item("comment", self.comment.pk, content_hash("hello"))
        self.assertEqual(result["skipped"], "stale or private")
        call.assert_not_called()

    @patch(
        "judge.tasks.omni_moderation.moderate_content",
        side_effect=OmniModerationError("unavailable"),
    )
    @patch.object(moderate_omni_item, "retry", side_effect=Retry())
    def test_api_failure_retries_without_marking_content_reviewed(self, retry, call):
        with self.assertRaises(Retry):
            moderate_omni_item("chat", self.message.pk, content_hash("hello"))
        self.assertFalse(
            ChatModerationLog.objects.filter(message=self.message).exists()
        )

    @patch(
        "judge.tasks.omni_moderation.moderate_content",
        side_effect=OmniModerationError("unavailable"),
    )
    def test_exhausted_retries_queue_muse(self, call):
        result = moderate_omni_item.apply(
            args=("chat", self.message.pk, content_hash("hello")), retries=4, throw=True
        ).get()
        self.assertTrue(result["flagged"])
        log = ChatModerationLog.objects.get(message=self.message)
        self.assertEqual(log.action, "review")
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)

    def test_durable_case_rolls_back_with_content_transaction(self):
        with transaction.atomic():
            self.assertTrue(record_omni_pending("chat", self.message))
            transaction.set_rollback(True)
        self.assertFalse(
            ChatModerationLog.objects.filter(message=self.message).exists()
        )

    @patch("judge.tasks.omni_moderation.enqueue_omni")
    def test_recovery_queues_durable_case_after_long_outage(self, enqueue):
        self.assertTrue(record_omni_pending("chat", self.message))
        ChatModerationLog.objects.filter(message=self.message).update(
            created_at=timezone.now() - timezone.timedelta(days=4)
        )
        Message.objects.filter(pk=self.message.pk).update(
            time=timezone.now() - timezone.timedelta(days=4)
        )
        self.assertEqual(recover_omni_items("chat"), {"queued": 1})
        enqueue.assert_called_once_with("chat", self.message.pk, self.message.body)

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_durable_record_is_resolved_without_duplicate_log(self, call):
        self.assertTrue(record_omni_pending("chat", self.message))
        call.return_value = {
            "flagged": True,
            "categories": {"harassment": True},
            "category_scores": {"harassment": 0.4},
        }
        moderate_omni_item("chat", self.message.pk, content_hash(self.message.body))
        log = ChatModerationLog.objects.get(message=self.message)
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)

    def test_edit_supersedes_earlier_durable_comment_check(self):
        self.assertTrue(record_omni_pending("comment", self.comment))
        old = CommentModerationLog.objects.get(comment=self.comment)
        self.comment.body = "new content"
        self.comment.save(update_fields=["body"])
        self.assertTrue(record_omni_pending("comment", self.comment))
        old.refresh_from_db()
        self.assertEqual(old.muse_status, MuseStatus.SUPERSEDED)
        self.assertEqual(
            CommentModerationLog.objects.filter(
                comment=self.comment, muse_status=MuseStatus.PENDING_OMNI
            ).count(),
            1,
        )
        self.comment.body = "hello"
        self.comment.save(update_fields=["body"])
        self.assertTrue(record_omni_pending("comment", self.comment))
        self.assertEqual(
            CommentModerationLog.objects.filter(
                comment=self.comment,
                content_hash=content_hash("hello"),
                muse_status=MuseStatus.PENDING_OMNI,
            ).count(),
            1,
        )

    @patch("judge.tasks.omni_moderation.enqueue_omni")
    def test_recovery_finds_unreviewed_and_edited_content(self, enqueue):
        CommentModerationLog.log_action(
            comment=self.comment, action="keep", is_automated=True
        )
        Comment.objects.filter(pk=self.comment.pk).update(body="edited")
        self.assertEqual(recover_omni_items("comment"), {"queued": 1})
        enqueue.assert_called_once_with("comment", self.comment.pk, "edited")

    @patch("judge.tasks.omni_moderation.moderate_content")
    def test_human_decision_during_api_call_is_not_overwritten(self, call):
        def respond(*args):
            ChatModerationLog.log_action(
                message=self.message, action="keep", is_automated=False
            )
            return {"flagged": True, "categories": {"violence": True}}

        call.side_effect = respond
        self.assertEqual(
            moderate_omni_item("chat", self.message.pk, content_hash("hello"))[
                "skipped"
            ],
            "already reviewed",
        )
        self.assertEqual(
            ChatModerationLog.objects.filter(message=self.message).count(), 1
        )

    @patch("chat_box.views.broadcast_room_event")
    @patch("chat_box.views.enqueue_omni")
    @patch("chat_box.views.check_valid_message", return_value=True)
    def test_new_lobby_message_queues_immediately_after_commit(
        self, validate, enqueue, broadcast
    ):
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("post_chat_message"),
                {"room": self.room.pk, "body": "new message"},
            )
        self.assertEqual(response.status_code, 200, response.content)
        message = Message.objects.get(room=self.room, body="new message")
        enqueue.assert_called_once_with("chat", message.pk, message.body)
        self.assertEqual(
            ChatModerationLog.objects.get(message=message).muse_status,
            MuseStatus.PENDING_OMNI,
        )

    @patch("judge.views.comment.forms.can_use_community_features", return_value=True)
    @patch("judge.views.comment.actions.can_use_community_features", return_value=True)
    @patch("judge.views.comment.actions.enqueue_omni")
    def test_new_comment_queues_immediately_after_commit(
        self, enqueue, allowed, form_allowed
    ):
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("comment_post"),
                {
                    "content_type_id": self.comment.content_type_id,
                    "object_id": self.blog.pk,
                    "body": "new comment",
                },
            )
        self.assertEqual(response.status_code, 200)
        comment = Comment.objects.get(body="new comment")
        enqueue.assert_called_once_with("comment", comment.pk, comment.body)
        self.assertEqual(
            CommentModerationLog.objects.get(comment=comment).muse_status,
            MuseStatus.PENDING_OMNI,
        )

    @patch("judge.views.comment.edit_views.enqueue_omni")
    def test_edited_comment_queues_new_content(self, enqueue):
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("comment_edit", kwargs={"id": self.comment.pk}),
                {"body": "updated comment"},
            )
        self.assertEqual(response.status_code, 302, response.content)
        enqueue.assert_called_once_with("comment", self.comment.pk, "updated comment")
