from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from judge.review.llm import call_llm_json, LLMCallFailed


@override_settings(AUTO_REVIEW_LLM_RETRY_COUNT=2)
class CallLLMJsonTest(TestCase):
    @patch("judge.review.llm.LLMService")
    @patch("judge.review.llm.get_config")
    def test_returns_parsed_json_on_success(self, mock_config, mock_service):
        mock_config.return_value = MagicMock(api_key="k", bot_name="b")
        mock_service.return_value.call_llm.return_value = '{"verdict": "correct"}'
        result = call_llm_json("system", "user")
        self.assertEqual(result, {"verdict": "correct"})

    @patch("judge.review.llm.LLMService")
    @patch("judge.review.llm.get_config")
    def test_strips_markdown_fences(self, mock_config, mock_service):
        mock_config.return_value = MagicMock(api_key="k", bot_name="b")
        mock_service.return_value.call_llm.return_value = '```json\n{"a": 1}\n```'
        result = call_llm_json("system", "user")
        self.assertEqual(result, {"a": 1})

    @patch("judge.review.llm.LLMService")
    @patch("judge.review.llm.get_config")
    def test_retries_then_raises(self, mock_config, mock_service):
        mock_config.return_value = MagicMock(api_key="k", bot_name="b")
        mock_service.return_value.call_llm.side_effect = RuntimeError("boom")
        with self.assertRaises(LLMCallFailed):
            call_llm_json("system", "user")
        # 2 retries
        self.assertEqual(mock_service.return_value.call_llm.call_count, 2)
