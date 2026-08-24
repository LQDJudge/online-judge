from io import StringIO
from unittest.mock import MagicMock, call, patch

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from chat_box.models import ChatModerationLog, Message
from judge.management.commands.auto_moderate import (
    CHAT_SYSTEM_PROMPT,
    COMMENT_SYSTEM_PROMPT,
)
from judge.models import (
    BlogPost,
    Comment,
    CommentModerationLog,
    Language,
    Organization,
    OrganizationModerationLog,
    Profile,
)
from judge.models.comment import get_comment_context_details, mute_comment_author


class ChatModerationPromptTest(SimpleTestCase):
    def test_prompt_is_strict_for_harmful_content_but_tolerant_of_jokes(self):
        self.assertIn("Be strict for harmful", CHAT_SYSTEM_PROMPT)
        self.assertIn("obvious jokes", CHAT_SYSTEM_PROMPT)
        self.assertIn("mild profanity without a target", CHAT_SYSTEM_PROMPT)
        self.assertIn("adult/sexual", CHAT_SYSTEM_PROMPT)
        self.assertIn("gambling", CHAT_SYSTEM_PROMPT)
        self.assertIn("When in doubt, KEEP", CHAT_SYSTEM_PROMPT)
        self.assertIn("If you cannot see an image", CHAT_SYSTEM_PROMPT)

    def test_prompt_allows_benign_community_links(self):
        self.assertIn("Sharing links is allowed", CHAT_SYSTEM_PROMPT)
        self.assertIn("Discord or other group chats", CHAT_SYSTEM_PROMPT)
        self.assertIn("LQDOJ organizations", CHAT_SYSTEM_PROMPT)
        self.assertIn("Do not classify a link as promotional spam", CHAT_SYSTEM_PROMPT)
        self.assertIn("If you cannot verify the destination", CHAT_SYSTEM_PROMPT)

    def test_comment_prompt_mirrors_chat_policy_with_review_action(self):
        self.assertIn("educational programming site", COMMENT_SYSTEM_PROMPT)
        self.assertIn('"review"', COMMENT_SYSTEM_PROMPT)
        self.assertIn("obvious jokes", COMMENT_SYSTEM_PROMPT)
        self.assertIn("Discord or other group chats", COMMENT_SYSTEM_PROMPT)
        self.assertIn("When in doubt, KEEP", COMMENT_SYSTEM_PROMPT)


class AutoModerateCommandTest(TestCase):
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

    def create_comment(self, body="Needs a human look"):
        user = User.objects.create_user("comment_user", password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        post = BlogPost.objects.create(
            title="Visible post",
            slug="visible-post",
            visible=True,
            publish_on=timezone.now(),
            content="Post body",
        )
        return Comment.objects.create(
            author=profile,
            content_type=ContentType.objects.get_for_model(BlogPost),
            object_id=post.id,
            body=body,
        )

    def test_comment_context_details_batch_blog_lookup(self):
        user = User.objects.create_user("context_comment_user", password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        post = BlogPost.objects.create(
            title="Visible post",
            slug="visible-post-context",
            visible=True,
            publish_on=timezone.now(),
            content="Post body",
        )
        content_type = ContentType.objects.get_for_model(BlogPost)
        comments = [
            Comment.objects.create(
                author=profile,
                content_type=content_type,
                object_id=post.id,
                body="Comment %d" % index,
            )
            for index in range(4)
        ]
        comments = list(
            Comment.objects.filter(id__in=[comment.id for comment in comments])
            .select_related("content_type")
            .order_by("id")
        )

        get_comment_context_details(comments[:1])
        with CaptureQueriesContext(connection) as queries:
            details = get_comment_context_details(comments)

        self.assertLessEqual(len(queries), 1)
        self.assertEqual(set(details), {comment.id for comment in comments})
        for comment in comments:
            self.assertEqual(details[comment.id]["prompt_label"], "Post: Visible post")
            self.assertIn("#comment-%d" % comment.id, details[comment.id]["url"])

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_chat_moderation_uses_moderation_bot_config(self, get_config, llm_service):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        get_config.return_value = FakeConfig()

        call_command(
            "auto_moderate",
            "--chat-only",
            "--dry-run",
            stdout=StringIO(),
        )

        llm_service.assert_has_calls(
            [
                call(api_key="test-key", bot_name="Qwen3.7-Flash-EL"),
                call(
                    api_key="test-key",
                    bot_name="Qwen3.7-Flash-EL",
                    sleep_time=0.5,
                    timeout=30,
                ),
            ]
        )

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_comments_only_logs_review_decision(self, get_config, llm_service):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        comment = self.create_comment()
        get_config.return_value = FakeConfig()
        comment_service = MagicMock()
        chat_service = MagicMock()
        comment_service.call_llm.return_value = (
            '[{"id": %d, "action": "review", "reason": "Needs context"}]' % comment.id
        )
        llm_service.side_effect = [comment_service, chat_service]

        call_command(
            "auto_moderate",
            "--comments-only",
            stdout=StringIO(),
        )

        log = CommentModerationLog.objects.get(comment=comment)
        self.assertEqual(log.action, CommentModerationLog.ACTION_REVIEW)
        self.assertEqual(log.reason, "Needs context")
        self.assertTrue(log.is_automated)
        comment.refresh_from_db()
        self.assertFalse(comment.hidden)

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_chat_moderation_logs_review_decision(self, get_config, llm_service):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        user = User.objects.create_user("chat_review_user", password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        message = Message.objects.create(
            room=None,
            author=profile,
            body="This needs context before hiding",
        )
        get_config.return_value = FakeConfig()
        unused_service = MagicMock()
        chat_service = MagicMock()
        chat_service.call_llm.return_value = (
            '[{"id": %d, "action": "review", "reason": "Ambiguous context"}]'
            % message.id
        )
        llm_service.side_effect = [unused_service, chat_service]

        call_command(
            "auto_moderate",
            "--chat-only",
            stdout=StringIO(),
        )

        log = ChatModerationLog.objects.get(message=message)
        self.assertEqual(log.action, "review")
        self.assertEqual(log.reason, "Ambiguous context")
        self.assertTrue(log.is_automated)
        message.refresh_from_db()
        self.assertFalse(message.hidden)

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_chat_moderation_accepts_string_ids_and_custom_window(
        self, get_config, llm_service
    ):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        user = User.objects.create_user("chat_string_id_user", password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        message = Message.objects.create(
            room=None,
            author=profile,
            body="Older message still inside custom moderation window",
        )
        Message.objects.filter(id=message.id).update(
            time=timezone.now() - timezone.timedelta(minutes=90)
        )
        get_config.return_value = FakeConfig()
        unused_service = MagicMock()
        chat_service = MagicMock()
        chat_service.call_llm.return_value = (
            '[{"id": "%d", "action": "keep"}]' % message.id
        )
        llm_service.side_effect = [unused_service, chat_service]

        call_command(
            "auto_moderate",
            "--chat-only",
            "--chat-window-minutes",
            "120",
            stdout=StringIO(),
        )

        log = ChatModerationLog.objects.get(message=message)
        self.assertEqual(log.action, "keep")
        self.assertTrue(log.is_automated)

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_chat_moderation_reports_missing_batch_decisions(
        self, get_config, llm_service
    ):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        user = User.objects.create_user("chat_missing_decision_user", password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        first = Message.objects.create(room=None, author=profile, body="First message")
        second = Message.objects.create(
            room=None, author=profile, body="Second message"
        )
        get_config.return_value = FakeConfig()
        unused_service = MagicMock()
        chat_service = MagicMock()
        chat_service.call_llm.return_value = '[{"id": %d, "action": "keep"}]' % first.id
        llm_service.side_effect = [unused_service, chat_service]
        output = StringIO()

        call_command("auto_moderate", "--chat-only", stdout=output)

        self.assertTrue(ChatModerationLog.objects.filter(message=first).exists())
        self.assertFalse(ChatModerationLog.objects.filter(message=second).exists())
        self.assertIn("Missing moderation results for chat messages", output.getvalue())
        self.assertIn("Errors: 1", output.getvalue())

    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_post_moderation_skips_admin_authored_posts(self, get_config, llm_service):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "Qwen3.7-Flash-EL"

        admin_user = User.objects.create_superuser("admin_author", password="pw")
        normal_user = User.objects.create_user("normal_post_author", password="pw")
        admin_profile, _ = Profile.objects.get_or_create(
            user=admin_user, defaults={"language": self.language}
        )
        normal_profile, _ = Profile.objects.get_or_create(
            user=normal_user, defaults={"language": self.language}
        )
        org = Organization.objects.create(
            name="Auto Mod Community",
            slug="auto-mod-community",
            short_name="AMC",
            about="A test community",
            registrant=normal_profile,
            is_community=True,
        )
        admin_post = BlogPost.objects.create(
            title="Admin pending post",
            slug="admin-pending-post",
            visible=False,
            publish_on=timezone.now(),
            content="Admin content should stay pending for manual review.",
            is_organization_private=True,
            is_rejected=False,
        )
        admin_post.authors.add(admin_profile)
        admin_post.organizations.add(org)
        normal_post = BlogPost.objects.create(
            title="Normal pending post",
            slug="normal-pending-post",
            visible=False,
            publish_on=timezone.now(),
            content="Normal content can be auto moderated.",
            is_organization_private=True,
            is_rejected=False,
        )
        normal_post.authors.add(normal_profile)
        normal_post.organizations.add(org)

        get_config.return_value = FakeConfig()
        post_service = MagicMock()
        unused_chat_service = MagicMock()
        post_service.call_llm.return_value = (
            '[{"id": %d, "action": "approve"}]' % normal_post.id
        )
        llm_service.side_effect = [post_service, unused_chat_service]

        call_command("auto_moderate", "--posts-only", stdout=StringIO())

        prompt = post_service.call_llm.call_args.args[0]
        self.assertIn("Normal pending post", prompt)
        self.assertNotIn("Admin pending post", prompt)
        admin_post.refresh_from_db()
        normal_post.refresh_from_db()
        self.assertFalse(admin_post.visible)
        self.assertTrue(normal_post.visible)
        self.assertFalse(
            OrganizationModerationLog.objects.filter(object_id=admin_post.id).exists()
        )

    def test_chat_moderation_search_matches_message_body(self):
        admin = User.objects.create_superuser("chat_mod_admin", password="pw")
        Profile.objects.get_or_create(user=admin, defaults={"language": self.language})
        matching_user = User.objects.create_user("matching_chat_author", password="pw")
        other_user = User.objects.create_user("other_chat_author", password="pw")
        matching_profile, _ = Profile.objects.get_or_create(
            user=matching_user, defaults={"language": self.language}
        )
        other_profile, _ = Profile.objects.get_or_create(
            user=other_user, defaults={"language": self.language}
        )
        matching_message = Message.objects.create(
            room=None,
            author=matching_profile,
            body="This message contains needlebody text",
        )
        other_message = Message.objects.create(
            room=None,
            author=other_profile,
            body="This message should not match",
        )
        ChatModerationLog.objects.create(
            message=matching_message,
            action="review",
            reason="Needs review",
            is_automated=True,
        )
        ChatModerationLog.objects.create(
            message=other_message,
            action="review",
            reason="Needs review",
            is_automated=True,
        )

        client = Client()
        client.login(username="chat_mod_admin", password="pw")
        response = client.get("/internal/chat_moderation", {"search": "needlebody"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "matching_chat_author")
        self.assertNotContains(response, "other_chat_author")

    def test_comment_mute_uses_shared_profile_mute_fields(self):
        comment = self.create_comment("Repeated targeted insult")

        result = mute_comment_author(
            comment,
            reason="Repeated harassment",
            is_automated=True,
            mute_type="temporary",
        )

        comment.author.refresh_from_db()
        comment.refresh_from_db()
        log = CommentModerationLog.objects.get(comment=comment)

        self.assertTrue(comment.author.mute)
        self.assertIsNotNone(comment.author.mute_until)
        self.assertEqual(comment.author.mute_reason, "Repeated harassment")
        self.assertTrue(comment.hidden)
        self.assertEqual(result["action"], CommentModerationLog.ACTION_MUTE_TEMP)
        self.assertEqual(log.action, CommentModerationLog.ACTION_MUTE_TEMP)
        self.assertEqual(log.mute_duration_days, 1)

    def test_comment_mute_endpoint_hides_comment_and_uses_shared_mute(self):
        comment = self.create_comment("Bad comment visible on page")
        mod_user = User.objects.create_user("comment_mod", password="pw")
        Profile.objects.get_or_create(
            user=mod_user, defaults={"language": self.language}
        )
        mod_user.user_permissions.add(Permission.objects.get(codename="change_comment"))

        client = Client()
        client.login(username="comment_mod", password="pw")
        response = client.post(
            "/comments/mute/",
            {
                "id": comment.id,
                "mute_type": "temporary",
                "reason": "Bad comment",
            },
        )

        self.assertEqual(response.status_code, 200)
        comment.refresh_from_db()
        comment.author.refresh_from_db()
        log = CommentModerationLog.objects.get(comment=comment)

        self.assertTrue(comment.hidden)
        self.assertTrue(comment.author.mute)
        self.assertIsNotNone(comment.author.mute_until)
        self.assertEqual(log.action, CommentModerationLog.ACTION_MUTE_TEMP)

    def test_muted_user_gets_readable_comment_post_error(self):
        muted_user = User.objects.create_user("muted_commenter", password="pw")
        muted_profile, _ = Profile.objects.get_or_create(
            user=muted_user, defaults={"language": self.language}
        )
        muted_profile.mute = True
        muted_profile.save(update_fields=["mute"])
        post = BlogPost.objects.create(
            title="Muted post target",
            slug="muted-post-target",
            visible=True,
            publish_on=timezone.now(),
            content="Post body",
        )

        client = Client()
        client.login(username="muted_commenter", password="pw")
        response = client.post(
            "/comments/post/",
            {
                "parent": "",
                "body": "I should not be able to post this",
                "content_type_id": ContentType.objects.get_for_model(BlogPost).id,
                "object_id": post.id,
            },
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "You are muted and cannot comment.")

        response = client.post(
            "/comments/post/",
            {
                "parent": "",
                "body": "I should not be able to post this",
                "content_type_id": ContentType.objects.get_for_model(BlogPost).id,
                "object_id": post.id,
            },
            HTTP_ACCEPT_LANGUAGE="vi",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.content.decode(), "Bạn đang bị cấm và không thể bình luận."
        )
        self.assertFalse(
            Comment.objects.filter(author=muted_profile, object_id=post.id).exists()
        )
