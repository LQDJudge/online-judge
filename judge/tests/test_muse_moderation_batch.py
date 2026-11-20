import json
from threading import Event, Thread
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from chat_box.models import ChatModerationLog, Message, Room
from judge.management.commands.auto_moderate import Command
from judge.models import (
    BlogPost,
    Comment,
    CommentModerationLog,
    Language,
    Problem,
    Profile,
    Solution,
    hide_comment_for_moderation,
)
from judge.moderation_status import MuseStatus
from judge.models.notification import Notification, NotificationCategory
from judge.moderation_images import ModerationImageError
from judge.tasks.omni_moderation import content_hash, moderate_omni_item


class MuseModerationBatchTests(TestCase):
    def setUp(self):
        language, _ = Language.objects.get_or_create(
            key="PY3", defaults={"name": "Python 3", "common_name": "Python"}
        )
        user = User.objects.create_user(username="muse_batch_test")
        self.profile = Profile.objects.create(user=user, language=language)
        room = Room.objects.get(singleton_key="lobby")
        self.message = Message.objects.create(
            room=room, author=self.profile, body="test content"
        )
        blog = BlogPost.objects.create(
            title="test",
            slug="muse-test",
            content="test",
            publish_on=timezone.now(),
            visible=True,
        )
        self.comment = Comment.objects.create(
            author=self.profile,
            body="test content",
            content_type=ContentType.objects.get_for_model(BlogPost),
            object_id=blog.pk,
        )
        self.command = Command()
        self.command.batch_size = 40
        self.command.dry_run = False
        self.command.llm_service = Mock()
        self.command.chat_llm_service = Mock()

    def queue(self, kind, high=False):
        obj = self.message if kind == "chat" else self.comment
        result = {
            "flagged": True,
            "categories": {"violence/graphic": True},
            "category_scores": {"violence/graphic": 0.75 if high else 0.5},
            "category_applied_input_types": {"violence/graphic": ["text"]},
        }
        with patch("judge.tasks.omni_moderation.moderate_content", return_value=result):
            moderate_omni_item(kind, obj.pk, content_hash(obj.body))
        model = ChatModerationLog if kind == "chat" else CommentModerationLog
        return model.objects.get(
            **{"message" if kind == "chat" else "comment": obj},
            muse_status__in=[MuseStatus.PENDING_VISIBLE, MuseStatus.PENDING_HIDDEN],
        )

    def decision(self, kind, log, action, reason=""):
        service = (
            self.command.chat_llm_service
            if kind == "chat"
            else self.command.llm_service
        )
        service.call_llm.return_value = json.dumps(
            [{"id": log.pk, "action": action, "reason": reason}]
        )
        return self.command._moderate_pending(kind)

    def test_omni_hidden_muse_keep_restores_both_types(self):
        for kind in ("chat", "comment"):
            log = self.queue(kind, high=True)
            obj = self.message if kind == "chat" else self.comment
            obj.refresh_from_db()
            self.assertTrue(obj.hidden)
            self.assertEqual(log.muse_status, MuseStatus.PENDING_HIDDEN)
            self.decision(kind, log, "keep")
            obj.refresh_from_db()
            log.refresh_from_db()
            self.assertFalse(obj.hidden)
            self.assertEqual(log.muse_status, MuseStatus.COMPLETED)
            self.assertEqual(log.action, "keep")

    def test_muse_keep_does_not_restore_reply_hidden_by_moderator(self):
        parent = Comment.objects.create(
            author=self.profile,
            body="parent",
            content_type=self.comment.content_type,
            object_id=self.comment.object_id,
        )
        self.comment = Comment.objects.create(
            author=self.profile,
            body="reply",
            content_type=parent.content_type,
            object_id=parent.object_id,
            parent=parent,
        )
        log = self.queue("comment", high=True)
        hide_comment_for_moderation(parent, reason="Vi phạm", is_automated=False)
        self.decision("comment", log, "keep")
        self.comment.refresh_from_db()
        log.refresh_from_db()
        self.assertTrue(self.comment.hidden)
        self.assertEqual(log.muse_status, MuseStatus.COMPLETED)

    def test_hiding_parent_and_keeping_reply_in_same_batch_preserves_subtree(self):
        parent = self.comment
        reply = Comment.objects.create(
            author=self.profile,
            body="reply",
            content_type=parent.content_type,
            object_id=parent.object_id,
            parent=parent,
        )
        parent_log = self.queue("comment")
        self.comment = reply
        reply_log = self.queue("comment", high=True)
        self.command.llm_service.call_llm.return_value = json.dumps(
            [
                {"id": parent_log.pk, "action": "hide", "reason": "Vi phạm"},
                {"id": reply_log.pk, "action": "keep", "reason": ""},
            ]
        )
        self.command.moderate_comments()
        reply.refresh_from_db()
        reply_log.refresh_from_db()
        self.assertTrue(reply.hidden)
        self.assertEqual(reply_log.muse_status, MuseStatus.COMPLETED)

    def test_private_editorial_comment_is_not_sent_to_muse(self):
        problem = Problem.objects.create(
            code="museprivate",
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
        CommentModerationLog.log_action(
            comment=self.comment,
            action="review",
            is_automated=True,
            content_hash=content_hash(self.comment.body),
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        self.command.moderate_comments()
        self.command.llm_service.call_llm.assert_not_called()

    def test_keep_batch_queries_do_not_scale_per_message(self):
        logs = []
        for number in range(10):
            message = Message.objects.create(
                room=self.message.room, author=self.profile, body="message %d" % number
            )
            logs.append(
                ChatModerationLog.log_action(
                    message=message,
                    action="review",
                    is_automated=True,
                    muse_status=MuseStatus.PENDING_VISIBLE,
                )
            )
        self.command.chat_llm_service.call_llm.return_value = json.dumps(
            [{"id": log.pk, "action": "keep", "reason": ""} for log in logs]
        )
        with CaptureQueriesContext(connection) as queries:
            self.command.moderate_chat()
        self.assertLess(len(queries), 20)
        self.assertEqual(
            ChatModerationLog.objects.filter(
                pk__in=[log.pk for log in logs], muse_status=MuseStatus.COMPLETED
            ).count(),
            10,
        )

    def test_moderator_can_restore_hidden_pending_case(self):
        self.profile.user.is_superuser = True
        self.profile.user.is_staff = True
        self.profile.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.profile.user)
        for kind in ("chat", "comment"):
            log = self.queue(kind, high=True)
            obj = self.message if kind == "chat" else self.comment
            response = self.client.post(
                reverse(
                    "internal_chat_moderation"
                    if kind == "chat"
                    else "internal_comment_moderation"
                ),
                {"log": log.pk, "action": "keep"},
            )
            self.assertEqual(response.status_code, 302)
            obj.refresh_from_db()
            log.refresh_from_db()
            self.assertFalse(obj.hidden)
            self.assertFalse(log.is_automated)
            self.assertEqual(log.muse_status, MuseStatus.NONE)

    def test_moderator_resolving_older_comment_log_cancels_new_pending_case(self):
        self.profile.user.is_superuser = True
        self.profile.user.is_staff = True
        self.profile.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.profile.user)
        old_log = CommentModerationLog.log_action(
            comment=self.comment,
            action="review",
            is_automated=True,
            content_hash=content_hash("older body"),
        )
        pending = self.queue("comment", high=True)
        response = self.client.post(
            reverse("internal_comment_moderation"),
            {"log": old_log.pk, "action": "keep"},
        )
        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.comment.refresh_from_db()
        self.assertEqual(pending.muse_status, MuseStatus.NONE)
        self.assertFalse(self.comment.hidden)
        self.command.moderate_comments()
        self.command.llm_service.call_llm.assert_not_called()

    def test_visible_muse_hide_comment(self):
        log = self.queue("comment")
        self.decision("comment", log, "hide", "Nội dung xúc phạm")
        self.comment.refresh_from_db()
        log.refresh_from_db()
        self.assertTrue(self.comment.hidden)
        self.assertEqual(log.reason, "Nội dung xúc phạm")
        self.assertEqual(log.muse_status, MuseStatus.COMPLETED)
        notice = Notification.objects.filter(
            owner=self.profile, category=NotificationCategory.HIDE_COMMENT
        ).latest("pk")
        self.assertIn("Lý do", notice.html_link)
        self.assertIn("Nội dung xúc phạm", notice.html_link)

    def test_existing_permanent_mute_is_not_shortened(self):
        log = self.queue("chat")
        self.profile.mute = True
        self.profile.mute_until = None
        self.profile.save(update_fields=["mute", "mute_until"])
        self.decision("chat", log, "mute_temp", "Gửi tin rác")
        self.profile.refresh_from_db()
        self.message.refresh_from_db()
        log.refresh_from_db()
        self.assertTrue(self.profile.mute)
        self.assertIsNone(self.profile.mute_until)
        self.assertTrue(self.message.hidden)
        self.assertEqual(log.action, "hide")

    def test_two_mutes_in_one_batch_do_not_extend_duration_twice(self):
        first = self.queue("chat")
        other = Message.objects.create(
            room=self.message.room, author=self.profile, body="another bad message"
        )
        second = ChatModerationLog.log_action(
            message=other,
            action="review",
            is_automated=True,
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        self.command.chat_llm_service.call_llm.return_value = json.dumps(
            [
                {"id": first.pk, "action": "mute_temp", "reason": "Gửi tin rác"},
                {"id": second.pk, "action": "mute_temp", "reason": "Gửi tin rác"},
            ]
        )
        self.command.moderate_chat()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.action, "mute_temp")
        self.assertEqual(first.mute_duration_days, 1)
        self.assertEqual(second.action, "hide")
        self.assertEqual(second.muse_status, MuseStatus.COMPLETED)

    @patch("judge.management.commands.auto_moderate.mute_chat_user")
    def test_temporary_mute_uses_existing_duration_logic(self, mute):
        log = self.queue("chat")
        mute.return_value = {"mute_until": None, "mute_duration_days": 3}
        self.decision("chat", log, "mute_temp", "Gửi tin rác")
        self.assertEqual(mute.call_args.kwargs["mute_type"], "temporary")
        self.message.refresh_from_db()
        self.assertTrue(self.message.hidden)
        log.refresh_from_db()
        self.assertEqual(log.mute_duration_days, 3)

    @patch("judge.management.commands.auto_moderate.embed_moderation_images")
    def test_failed_image_is_deferred_so_other_cases_can_proceed(self, embed):
        first = self.queue("chat")
        second_message = Message.objects.create(
            room=self.message.room, author=self.profile, body="other message"
        )
        second = ChatModerationLog.log_action(
            message=second_message,
            action="review",
            is_automated=True,
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        self.command.batch_size = 1
        embed.side_effect = ModerationImageError("test failure")
        self.command.moderate_chat()
        first.refresh_from_db()
        self.assertGreater(first.muse_retry_after, timezone.now())
        embed.side_effect = None
        embed.return_value = ("other message", [])
        self.decision("chat", second, "keep")
        second.refresh_from_db()
        self.assertEqual(second.muse_status, MuseStatus.COMPLETED)
        first.refresh_from_db()
        self.assertEqual(first.muse_status, MuseStatus.PENDING_VISIBLE)

    @patch("judge.management.commands.auto_moderate.embed_moderation_images")
    def test_repeated_image_failure_escalates_hidden_case_to_dashboard(self, embed):
        log = self.queue("chat", high=True)
        embed.side_effect = ModerationImageError("unavailable")
        self.command.moderate_chat()
        ChatModerationLog.objects.filter(pk=log.pk).update(
            muse_retry_after=timezone.now()
        )
        self.command.moderate_chat()
        log.refresh_from_db()
        self.message.refresh_from_db()
        self.assertTrue(self.message.hidden)
        self.assertEqual(log.action, "review")
        self.assertEqual(log.muse_status, MuseStatus.NONE)
        self.assertIn("hình ảnh", log.reason)
        self.assertIsNone(log.muse_retry_after)
        self.profile.user.is_superuser = True
        self.profile.user.is_staff = True
        self.profile.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.profile.user)
        response = self.client.get(reverse("internal_chat_moderation"))
        self.assertContains(response, self.message.body)

    def test_duplicate_muse_decisions_remain_pending(self):
        log = self.queue("chat")
        self.command.chat_llm_service.call_llm.return_value = json.dumps(
            [
                {"id": log.pk, "action": "keep", "reason": ""},
                {"id": log.pk, "action": "hide", "reason": "Vi phạm"},
            ]
        )
        stats = self.command.moderate_chat()
        log.refresh_from_db()
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)
        self.assertEqual(stats["errors"], 1)

    def test_missing_muse_decision_remains_pending(self):
        first = self.queue("chat")
        other = Message.objects.create(
            room=self.message.room, author=self.profile, body="other"
        )
        second = ChatModerationLog.log_action(
            message=other,
            action="review",
            is_automated=True,
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        stats = self.decision("chat", first, "keep")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.muse_status, MuseStatus.COMPLETED)
        self.assertEqual(second.muse_status, MuseStatus.PENDING_VISIBLE)
        self.assertEqual(stats["errors"], 1)

    def test_failed_response_keeps_case_pending(self):
        log = self.queue("chat")
        self.command.chat_llm_service.call_llm.return_value = "invalid"
        self.command.moderate_chat()
        log.refresh_from_db()
        self.assertEqual(log.muse_status, MuseStatus.PENDING_VISIBLE)

    def test_repeated_failed_response_escalates_hidden_case(self):
        log = self.queue("chat", high=True)
        self.command.chat_llm_service.call_llm.return_value = "invalid"
        self.command.moderate_chat()
        ChatModerationLog.objects.filter(pk=log.pk).update(
            muse_retry_after=timezone.now()
        )
        self.command.moderate_chat()
        log.refresh_from_db()
        self.message.refresh_from_db()
        self.assertTrue(self.message.hidden)
        self.assertEqual(log.action, "review")
        self.assertEqual(log.muse_status, MuseStatus.NONE)
        self.assertIn("kiểm duyệt", log.reason)

    @override_settings(POE_API_KEY="")
    def test_missing_service_configuration_escalates_after_retry(self):
        log = self.queue("chat", high=True)
        call_command("auto_moderate", "--chat-only")
        log.refresh_from_db()
        self.assertGreater(log.muse_retry_after, timezone.now())
        ChatModerationLog.objects.filter(pk=log.pk).update(
            muse_retry_after=timezone.now()
        )
        call_command("auto_moderate", "--chat-only")
        log.refresh_from_db()
        self.assertEqual(log.action, "review")
        self.assertEqual(log.muse_status, MuseStatus.NONE)

    def test_moderator_decision_during_call_wins(self):
        log = self.queue("chat")

        def call(*args, **kwargs):
            ChatModerationLog.objects.filter(pk=log.pk).update(
                action="keep", is_automated=False, muse_status=MuseStatus.COMPLETED
            )
            return json.dumps([{"id": log.pk, "action": "hide", "reason": "Vi phạm"}])

        self.command.chat_llm_service.call_llm.side_effect = call
        self.command.moderate_chat()
        self.message.refresh_from_db()
        self.assertFalse(self.message.hidden)

    def test_changed_comment_during_call_is_not_moderated(self):
        log = self.queue("comment")

        def call(*args, **kwargs):
            Comment.objects.filter(pk=self.comment.pk).update(body="changed")
            return json.dumps([{"id": log.pk, "action": "hide", "reason": "Vi phạm"}])

        self.command.llm_service.call_llm.side_effect = call
        self.command.moderate_comments()
        self.comment.refresh_from_db()
        self.assertFalse(self.comment.hidden)


class MuseModerationConcurrencyTests(TransactionTestCase):
    def test_moderator_decision_wins_while_muse_request_is_in_flight(self):
        if connection.vendor != "mysql":
            self.skipTest("Requires a database with row locking")
        language, _ = Language.objects.get_or_create(
            key="PY3", defaults={"name": "Python 3", "common_name": "Python"}
        )
        user = User.objects.create_user(username="muse_concurrent_test")
        profile = Profile.objects.create(user=user, language=language)
        message = Message.objects.create(
            room=Room.objects.get(singleton_key="lobby"), author=profile, body="test"
        )
        log = ChatModerationLog.log_action(
            message=message,
            action="review",
            is_automated=True,
            muse_status=MuseStatus.PENDING_VISIBLE,
        )
        started, resume = Event(), Event()
        failures = []

        def call(*args, **kwargs):
            started.set()
            if not resume.wait(10):
                raise RuntimeError("test timed out")
            return json.dumps([{"id": log.pk, "action": "hide", "reason": "Vi phạm"}])

        def run_batch():
            try:
                command = Command()
                command.batch_size = 10
                command.dry_run = False
                command.llm_service = Mock()
                command.chat_llm_service = Mock()
                command.chat_llm_service.call_llm.side_effect = call
                command.moderate_chat()
            except Exception as exc:
                failures.append(exc)
            finally:
                connections.close_all()

        thread = Thread(target=run_batch)
        thread.start()
        try:
            self.assertTrue(started.wait(10))
            ChatModerationLog.objects.filter(pk=log.pk).update(
                action="keep", is_automated=False, muse_status=MuseStatus.NONE
            )
        finally:
            resume.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        message.refresh_from_db()
        log.refresh_from_db()
        self.assertFalse(message.hidden)
        self.assertEqual(log.action, "keep")
        self.assertFalse(log.is_automated)
