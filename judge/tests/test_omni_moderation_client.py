from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from llm_service.omni_moderation import OmniModerationError, moderate_content


@override_settings(OPENAI_API_KEY="test-key")
class OmniModerationClientTest(SimpleTestCase):
    @patch("llm_service.omni_moderation.requests.post")
    def test_sends_text_and_image_and_returns_classification(self, post):
        result = {
            "flagged": True,
            "categories": {"violence": True},
            "category_scores": {"violence": 0.8},
        }
        post.return_value.json.return_value = {"results": [result]}
        self.assertEqual(
            moderate_content("hello", ["https://example.com/a.png"]), result
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "omni-moderation-latest")
        self.assertEqual(
            payload["input"][1]["image_url"]["url"], "https://example.com/a.png"
        )

    @patch("llm_service.omni_moderation.requests.post")
    def test_transport_and_invalid_responses_are_errors(self, post):
        post.side_effect = requests.Timeout("sensitive data")
        with self.assertRaisesMessage(
            OmniModerationError, "OpenAI moderation request failed"
        ):
            moderate_content("test")
        post.side_effect = None
        post.return_value = Mock()
        post.return_value.json.return_value = {"results": []}
        with self.assertRaises(OmniModerationError):
            moderate_content("test")
        post.return_value.json.return_value = {
            "results": [{"flagged": False, "categories": {"violence": "false"}}]
        }
        with self.assertRaises(OmniModerationError):
            moderate_content("test")

    @patch("llm_service.omni_moderation.requests.post")
    def test_missing_scores_are_not_treated_as_safe(self, post):
        post.return_value.json.return_value = {
            "results": [{"flagged": False, "categories": {"violence": False}}]
        }
        with self.assertRaises(OmniModerationError):
            moderate_content("test")
