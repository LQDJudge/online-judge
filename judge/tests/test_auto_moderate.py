from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from judge.management.commands.auto_moderate import CHAT_SYSTEM_PROMPT


class ChatModerationPromptTest(SimpleTestCase):
    def test_prompt_is_strict_for_harmful_content_but_tolerant_of_jokes(self):
        self.assertIn("Be strict for harmful", CHAT_SYSTEM_PROMPT)
        self.assertIn("obvious jokes", CHAT_SYSTEM_PROMPT)
        self.assertIn("mild profanity without a target", CHAT_SYSTEM_PROMPT)
        self.assertIn("When in doubt, KEEP", CHAT_SYSTEM_PROMPT)
        self.assertIn("If you cannot see an image", CHAT_SYSTEM_PROMPT)


class AutoModerateCommandTest(TestCase):
    @override_settings(POE_API_KEY="test-key", POE_BOT_NAME="Gemini-3-Flash")
    @patch("judge.management.commands.auto_moderate.LLMService")
    @patch("judge.management.commands.auto_moderate.get_config")
    def test_chat_moderation_uses_moderation_bot_config(self, get_config, llm_service):
        class FakeConfig:
            api_key = "test-key"
            sleep_time = 0.5
            timeout = 30

            def get_bot_name_for_moderation(self):
                return "gpt-5-nano"

        get_config.return_value = FakeConfig()

        call_command(
            "auto_moderate",
            "--chat-only",
            "--dry-run",
            stdout=StringIO(),
        )

        llm_service.assert_has_calls(
            [
                call(api_key="test-key", bot_name="Gemini-3-Flash"),
                call(
                    api_key="test-key",
                    bot_name="gpt-5-nano",
                    sleep_time=0.5,
                    timeout=30,
                ),
            ]
        )
