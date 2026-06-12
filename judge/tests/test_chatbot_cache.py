from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase

from judge.chatbot.cache import (
    clear_conversation,
    delete_message,
    get_cache_key,
    get_conversation,
    get_legacy_cache_key,
    save_conversation,
    set_model,
)
from judge.models import Problem
from judge.tasks.llm import _resolve_problem_for_ai_task


class ChatbotCacheKeyTest(TestCase):
    def setUp(self):
        cache.clear()
        self.config = MagicMock()
        self.config.get_chatbot_default_model.return_value = "test-bot"
        self.config.get_chatbot_supported_models.return_value = [
            {"id": "test-bot"},
            {"id": "new-bot"},
        ]

    def test_conversation_uses_problem_id_not_mutable_code(self):
        user_id = 7
        problem_id = 123
        old_code = "oldcode"
        new_code = "newcode"
        conversation = {
            "messages": [{"role": "user", "content": "hello"}],
            "model": "test-bot",
        }

        save_conversation(user_id, problem_id, conversation)

        with patch("judge.chatbot.cache.get_config", return_value=self.config):
            loaded = get_conversation(
                user_id,
                problem_id,
                legacy_problem_code=new_code,
            )

        self.assertEqual(loaded["messages"], conversation["messages"])
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, old_code)))
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, new_code)))

    def test_legacy_problem_code_cache_is_migrated_to_problem_id_key(self):
        user_id = 7
        problem_id = 123
        problem_code = "legacycode"
        conversation = {
            "messages": [{"role": "assistant", "content": "cached"}],
            "model": "test-bot",
        }
        cache.set(get_legacy_cache_key(user_id, problem_code), conversation)

        with patch("judge.chatbot.cache.get_config", return_value=self.config):
            loaded = get_conversation(
                user_id,
                problem_id,
                legacy_problem_code=problem_code,
            )

        self.assertEqual(loaded["messages"], conversation["messages"])
        self.assertEqual(cache.get(get_cache_key(user_id, problem_id)), conversation)
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, problem_code)))

    def test_clear_conversation_deletes_new_and_legacy_keys(self):
        user_id = 7
        problem_id = 123
        problem_code = "legacycode"
        conversation = {"messages": [], "model": "test-bot"}
        cache.set(get_cache_key(user_id, problem_id), conversation)
        cache.set(get_legacy_cache_key(user_id, problem_code), conversation)

        clear_conversation(user_id, problem_id, legacy_problem_code=problem_code)

        self.assertIsNone(cache.get(get_cache_key(user_id, problem_id)))
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, problem_code)))

    def test_set_model_migrates_legacy_conversation_before_saving(self):
        user_id = 7
        problem_id = 123
        problem_code = "legacycode"
        conversation = {
            "messages": [{"role": "user", "content": "keep me"}],
            "model": "test-bot",
        }
        cache.set(get_legacy_cache_key(user_id, problem_code), conversation)

        with patch("judge.chatbot.cache.get_config", return_value=self.config):
            self.assertTrue(
                set_model(
                    user_id,
                    problem_id,
                    "new-bot",
                    legacy_problem_code=problem_code,
                )
            )

        migrated = cache.get(get_cache_key(user_id, problem_id))
        self.assertEqual(migrated["messages"], conversation["messages"])
        self.assertEqual(migrated["model"], "new-bot")
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, problem_code)))

    def test_delete_message_migrates_legacy_conversation_before_saving(self):
        user_id = 7
        problem_id = 123
        problem_code = "legacycode"
        conversation = {
            "messages": [
                {"role": "user", "content": "delete me"},
                {"role": "assistant", "content": "delete me too"},
                {"role": "user", "content": "keep me"},
            ],
            "model": "test-bot",
        }
        cache.set(get_legacy_cache_key(user_id, problem_code), conversation)

        with patch("judge.chatbot.cache.get_config", return_value=self.config):
            self.assertTrue(
                delete_message(
                    user_id,
                    problem_id,
                    0,
                    legacy_problem_code=problem_code,
                )
            )

        migrated = cache.get(get_cache_key(user_id, problem_id))
        self.assertEqual(
            migrated["messages"],
            [{"role": "user", "content": "keep me"}],
        )
        self.assertIsNone(cache.get(get_legacy_cache_key(user_id, problem_code)))


class AiTaskProblemIdentityTest(TestCase):
    def test_problem_id_lookup_survives_problem_code_and_name_change(self):
        problem = Problem.objects.create(
            code="aitaskold",
            name="Old name",
            time_limit=1,
            memory_limit=65536,
            points=1,
        )

        problem.code = "aitasknew"
        problem.name = "New name"
        problem.save()

        resolved = _resolve_problem_for_ai_task(problem.id)

        self.assertEqual(resolved.id, problem.id)
        self.assertEqual(resolved.code, "aitasknew")

    def test_legacy_problem_code_lookup_still_works(self):
        problem = Problem.objects.create(
            code="legacytask",
            name="Legacy task",
            time_limit=1,
            memory_limit=65536,
            points=1,
        )

        resolved = _resolve_problem_for_ai_task("legacytask")

        self.assertEqual(resolved.id, problem.id)
