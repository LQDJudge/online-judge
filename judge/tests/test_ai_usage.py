from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from judge.models import (
    AIUsageLog,
    Language,
    Problem,
    ProblemGroup,
    ProblemType,
    Profile,
    ProfileModerationCase,
)
from judge.tasks.llm import tag_problem_task
from judge.tasks.username_moderation import moderate_profile_case_task
from llm_service import llm_api
from llm_service.llm_api import LLMService


def fake_llm_config():
    return SimpleNamespace(
        api_key="test-key",
        bot_name="TestBot",
        sleep_time=0,
        timeout=60,
        max_retries=1,
        get_bot_name_for_tagging=lambda: "TagBot",
        get_bot_name_for_markdown=lambda: "MarkdownBot",
        get_bot_name_for_solution=lambda: "SolutionBot",
        get_bot_name_for_moderation=lambda: "ModerationBot",
    )


class AIUsageLogTests(TestCase):
    def test_llm_service_records_successful_usage(self):
        user = get_user_model().objects.create_user("aiuser")
        service = LLMService(
            api_key="test-key",
            bot_name="TestBot",
            feature="problem_chatbot",
            user_id=user.id,
        )

        with patch.object(
            llm_api.fp,
            "get_bot_response_sync",
            return_value=[SimpleNamespace(text="answer", is_replace_response=False)],
        ):
            response = service.call_llm("prompt")

        self.assertEqual(response, "answer")
        log = AIUsageLog.objects.get()
        self.assertEqual(log.user, user)
        self.assertEqual(log.username, "aiuser")
        self.assertEqual(log.feature, "problem_chatbot")
        self.assertEqual(log.bot_name, "TestBot")
        self.assertEqual(log.status, AIUsageLog.STATUS_SUCCESS)
        self.assertEqual(log.input_chars, len("prompt"))
        self.assertEqual(log.output_chars, len("answer"))
        self.assertEqual(log.message_count, 1)
        self.assertIsNotNone(log.duration_ms)

    def test_llm_service_records_failed_usage(self):
        service = LLMService(
            api_key="test-key",
            bot_name="TestBot",
            feature="quiz_ai",
        )

        with patch.object(
            llm_api.fp,
            "get_bot_response_sync",
            side_effect=RuntimeError("poe unavailable"),
        ):
            response = service.call_llm("prompt")

        self.assertIsNone(response)
        log = AIUsageLog.objects.get()
        self.assertEqual(log.status, AIUsageLog.STATUS_ERROR)
        self.assertEqual(log.feature, "quiz_ai")
        self.assertIn("poe unavailable", log.error)

    def test_tag_problem_task_records_requesting_user(self):
        user = get_user_model().objects.create_user("tagger")
        language, _created = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python",
                "extension": "py",
            },
        )
        Profile.objects.create(user=user, language=language)
        group = ProblemGroup.objects.create(name="ai-usage", full_name="AI Usage")
        ProblemType.objects.create(name="math")
        problem = Problem.objects.create(
            code="aiusage",
            name="AI Usage",
            description="Read two integers and print their sum.",
            group=group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
        )

        with (
            patch("ai_features.problem_tag_service.get_config", fake_llm_config),
            patch.object(
                llm_api.fp,
                "get_bot_response_sync",
                return_value=[
                    SimpleNamespace(
                        text='{"is_valid": true, "points": 100, "tags": ["math"], "reason": null}',
                        is_replace_response=False,
                    )
                ],
            ),
        ):
            result = tag_problem_task(problem.id, user_id=user.id)

        self.assertTrue(result["success"])
        log = AIUsageLog.objects.get()
        self.assertEqual(log.user, user)
        self.assertEqual(log.username, "tagger")
        self.assertEqual(log.feature, "problem_tagging")
        self.assertEqual(log.bot_name, "TagBot")
        self.assertEqual(log.status, AIUsageLog.STATUS_SUCCESS)

    def test_profile_moderation_records_system_user_and_target_metadata(self):
        user = get_user_model().objects.create_user("needs_review")
        case = ProfileModerationCase.objects.create(
            user=user,
            username=user.username,
            target=ProfileModerationCase.TARGET_USERNAME,
            value_snapshot=user.username,
        )

        with (
            patch("judge.tasks.username_moderation.get_config", fake_llm_config),
            patch.object(
                llm_api.fp,
                "get_bot_response_sync",
                return_value=[
                    SimpleNamespace(
                        text=(
                            '{"decision": "allow", "category": "safe", '
                            '"confidence": 0.9, "reason": "ok"}'
                        ),
                        is_replace_response=False,
                    )
                ],
            ),
        ):
            result = moderate_profile_case_task(case.id)

        self.assertEqual(result["decision"], ProfileModerationCase.DECISION_ALLOW)
        log = AIUsageLog.objects.get()
        self.assertIsNone(log.user)
        self.assertEqual(log.username, "")
        self.assertEqual(log.feature, "username_moderation")
        self.assertEqual(log.bot_name, "ModerationBot")
        self.assertEqual(log.status, AIUsageLog.STATUS_SUCCESS)
        self.assertEqual(log.metadata["target_user_id"], user.id)
        self.assertEqual(log.metadata["target_username"], "needs_review")

    def test_profile_moderation_records_trigger_user_when_provided(self):
        user = get_user_model().objects.create_user("profile_owner")
        language, _created = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python",
                "extension": "py",
            },
        )
        Profile.objects.create(
            user=user, language=language, about="I like programming."
        )
        case = ProfileModerationCase.objects.create(
            user=user,
            username=user.username,
            target=ProfileModerationCase.TARGET_ABOUT,
            value_snapshot="I like programming.",
        )

        with (
            patch("judge.tasks.username_moderation.get_config", fake_llm_config),
            patch.object(
                llm_api.fp,
                "get_bot_response_sync",
                return_value=[
                    SimpleNamespace(
                        text=(
                            '{"decision": "allow", "category": "safe", '
                            '"confidence": 0.9, "reason": "ok"}'
                        ),
                        is_replace_response=False,
                    )
                ],
            ),
        ):
            result = moderate_profile_case_task(case.id, trigger_user_id=user.id)

        self.assertEqual(result["decision"], ProfileModerationCase.DECISION_ALLOW)
        log = AIUsageLog.objects.get()
        self.assertEqual(log.user, user)
        self.assertEqual(log.username, "profile_owner")
        self.assertEqual(log.feature, "profile_moderation")
        self.assertEqual(log.metadata["target_user_id"], user.id)

    def test_internal_ai_usage_page_requires_superuser(self):
        user_model = get_user_model()
        regular_user = user_model.objects.create_user("regular", password="pw")
        superuser = user_model.objects.create_superuser(
            "admin", "admin@example.com", "pw"
        )
        language, _created = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python",
                "extension": "py",
            },
        )
        Profile.objects.create(user=regular_user, language=language)
        Profile.objects.create(user=superuser, language=language)
        url = reverse("internal_ai_usage")

        anonymous_response = self.client.get(url)
        self.assertEqual(anonymous_response.status_code, 302)

        self.client.force_login(regular_user)
        regular_response = self.client.get(url)
        self.assertEqual(regular_response.status_code, 403)

        AIUsageLog.objects.create(
            user=regular_user,
            username=regular_user.username,
            feature="problem_chatbot",
            bot_name="TestBot",
            status=AIUsageLog.STATUS_SUCCESS,
            duration_ms=10,
            input_chars=6,
            output_chars=6,
            message_count=1,
        )
        AIUsageLog.objects.create(
            feature="profile_moderation",
            bot_name="TestBot",
            status=AIUsageLog.STATUS_SUCCESS,
            duration_ms=5,
            input_chars=4,
            output_chars=4,
            message_count=1,
        )

        self.client.force_login(superuser)
        superuser_response = self.client.get(url)
        self.assertEqual(superuser_response.status_code, 200)
        self.assertContains(superuser_response, 'value="problem_chatbot"')
        self.assertContains(superuser_response, 'value="problem_tagging"')
        self.assertContains(superuser_response, 'value="__system__"')
        self.assertContains(superuser_response, "Token")

        system_response = self.client.get(url, {"username": "__system__"})
        self.assertEqual(system_response.status_code, 200)
        system_logs = list(system_response.context["logs"])
        self.assertEqual(len(system_logs), 1)
        self.assertEqual(system_logs[0].feature, "profile_moderation")
