from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

import judge.models.profile as profile_models
from judge.models import Language, Profile, UsernameModerationCase
from judge.models.profile import get_profile_public_identity
from judge.tasks.username_moderation import (
    moderate_username_task,
    parse_username_moderation_response,
)
from llm_service import config as llm_config


class UsernameModerationTaskTest(TestCase):
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

    def setUp(self):
        llm_config._config = None
        self.addCleanup(setattr, llm_config, "_config", None)

    def test_parse_username_moderation_json_response(self):
        result = parse_username_moderation_response(
            '{"decision":"block","category":"gambling","confidence":0.97,'
            '"reason":"Gambling brand spam"}'
        )

        self.assertEqual(result["decision"], UsernameModerationCase.DECISION_BLOCK)
        self.assertEqual(result["category"], UsernameModerationCase.CATEGORY_GAMBLING)
        self.assertEqual(result["confidence"], 0.97)

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_high_confidence_block_disables_and_hides_user(self, call_llm):
        call_llm.return_value = (
            '{"decision":"block","category":"offensive","confidence":0.95,'
            '"reason":"Offensive username"}'
        )
        user = User.objects.create_user(username="badname")
        Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(user=user, username=user.username)

        result = moderate_username_task(case.id)

        user.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(result["decision"], UsernameModerationCase.DECISION_BLOCK)
        self.assertFalse(user.is_active)
        self.assertTrue(case.public_identity_hidden)
        self.assertEqual(case.status, UsernameModerationCase.STATUS_REVIEWED)

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_review_decision_stays_pending_for_human_review(self, call_llm):
        call_llm.return_value = (
            '{"decision":"review","category":"other","confidence":0.55,'
            '"reason":"Ambiguous joke"}'
        )
        user = User.objects.create_user(username="maybe_ok")
        Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(user=user, username=user.username)

        moderate_username_task(case.id)

        user.refresh_from_db()
        case.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(case.public_identity_hidden)
        self.assertEqual(case.status, UsernameModerationCase.STATUS_PENDING)
        self.assertEqual(case.decision, UsernameModerationCase.DECISION_REVIEW)

    @patch(
        "judge.tasks.username_moderation.get_config", side_effect=ValueError("no key")
    )
    def test_missing_llm_config_leaves_case_for_review(self, get_config):
        user = User.objects.create_user(username="config_missing")
        Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(user=user, username=user.username)

        result = moderate_username_task(case.id)

        case.refresh_from_db()
        self.assertIn("error", result)
        self.assertEqual(case.status, UsernameModerationCase.STATUS_PENDING)
        self.assertEqual(case.decision, UsernameModerationCase.DECISION_REVIEW)

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_safe_audit_case_can_be_deleted_after_ai_review(self, call_llm):
        call_llm.return_value = (
            '{"decision":"allow","category":"safe","confidence":0.92,'
            '"reason":"Safe username"}'
        )
        user = User.objects.create_user(username="safe_audit_user")
        Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            source=UsernameModerationCase.SOURCE_AUDIT,
        )

        result = moderate_username_task(case.id, delete_safe_case=True)

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(UsernameModerationCase.objects.filter(id=case.id).exists())


@override_settings(LANGUAGE_CODE="en")
class UsernameModerationDisplayTest(TestCase):
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

    def test_disabled_hidden_profile_shows_disabled_user_to_public(self):
        user = User.objects.create_user(username="offensive_name", is_active=False)
        profile = Profile.objects.create(
            user=user, language=self.language, about="public profile text"
        )
        UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            decision=UsernameModerationCase.DECISION_BLOCK,
            status=UsernameModerationCase.STATUS_REVIEWED,
            public_identity_hidden=True,
        )

        response = self.client.get(reverse("user_page", args=[user.username]))

        self.assertContains(response, "This user is disabled.")
        self.assertNotContains(response, "public profile text")
        self.assertEqual(profile.get_public_username(), "Disabled user")

    def test_active_hidden_profile_uses_disabled_user_public_name(self):
        user = User.objects.create_user(username="hidden_active_name", is_active=True)
        profile = Profile.objects.create(
            user=user, language=self.language, about="public profile text"
        )
        UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            decision=UsernameModerationCase.DECISION_REVIEW,
            status=UsernameModerationCase.STATUS_PENDING,
            public_identity_hidden=True,
        )

        response = self.client.get(reverse("user_page", args=[user.username]))

        self.assertContains(response, "Disabled user")
        self.assertNotContains(response, "public profile text")
        self.assertEqual(profile.get_public_username(), "Disabled user")

    def test_user_active_change_invalidates_public_identity_cache(self):
        user = User.objects.create_user(username="cache_active_user", is_active=True)
        profile = Profile.objects.create(user=user, language=self.language)

        self.assertFalse(profile.is_disabled())
        user.is_active = False
        user.save(update_fields=["is_active"])

        self.assertTrue(Profile.objects.get(id=profile.id).is_disabled())
        get_profile_public_identity.dirty(profile.id)

    def test_staff_can_see_hidden_disabled_username(self):
        user = User.objects.create_user(username="offensive_name", is_active=False)
        Profile.objects.create(
            user=user, language=self.language, about="public profile text"
        )
        UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            decision=UsernameModerationCase.DECISION_BLOCK,
            status=UsernameModerationCase.STATUS_REVIEWED,
            public_identity_hidden=True,
        )
        staff = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        Profile.objects.create(user=staff, language=self.language)
        self.client.login(username="admin", password="pw")

        response = self.client.get(reverse("user_page", args=[user.username]))

        self.assertContains(response, "offensive_name")
        self.assertContains(response, "public profile text")

    def test_public_identity_prefetch_keeps_username_rendering_constant_query(self):
        users = [
            User.objects.create_user(username="public_identity_%d" % i)
            for i in range(3)
        ]
        profiles = [
            Profile.objects.create(user=user, language=self.language) for user in users
        ]
        UsernameModerationCase.objects.create(
            user=users[1],
            username=users[1].username,
            decision=UsernameModerationCase.DECISION_BLOCK,
            status=UsernameModerationCase.STATUS_REVIEWED,
            public_identity_hidden=True,
        )
        profile_ids = [profile.id for profile in profiles]
        cache.clear()
        Language.get_default_language_pk()

        with self.assertNumQueries(1):
            cached_profiles = Profile.get_cached_instances(*profile_ids)
            Profile.prefetch_cache_public_identity(*profile_ids)
            usernames = [profile.get_public_username() for profile in cached_profiles]

        self.assertEqual(
            usernames,
            ["public_identity_0", "Disabled user", "public_identity_2"],
        )

    def test_legacy_profile_cache_without_identity_fields_falls_back_to_db(self):
        hidden_user = User.objects.create_user(
            username="legacy_cache_user", is_active=True
        )
        hidden_profile = Profile.objects.create(
            user=hidden_user, language=self.language
        )
        UsernameModerationCase.objects.create(
            user=hidden_user,
            username=hidden_user.username,
            public_identity_hidden=True,
        )
        inactive_user = User.objects.create_user(
            username="legacy_inactive_user", is_active=False
        )
        inactive_profile = Profile.objects.create(
            user=inactive_user, language=self.language
        )

        with patch.object(
            profile_models._get_profile,
            "batch",
            return_value=[
                {"username": hidden_user.username},
                {"username": inactive_user.username},
            ],
        ):
            with self.assertNumQueries(1):
                identities = profile_models._get_profile_public_identity_batch(
                    [(hidden_profile.id,), (inactive_profile.id,)]
                )

        self.assertEqual(
            identities,
            [
                {"is_active": True, "public_identity_hidden": True},
                {"is_active": False, "public_identity_hidden": False},
            ],
        )


@override_settings(LANGUAGE_CODE="en")
class UsernameModerationInternalViewTest(TestCase):
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

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        self.admin_profile = Profile.objects.create(
            user=self.admin, language=self.language
        )
        self.user = User.objects.create_user(username="case_user")
        Profile.objects.create(user=self.user, language=self.language)
        self.case = UsernameModerationCase.objects.create(
            user=self.user,
            username=self.user.username,
            decision=UsernameModerationCase.DECISION_REVIEW,
            category=UsernameModerationCase.CATEGORY_OTHER,
        )

    def test_internal_page_lists_cases(self):
        allowed_user = User.objects.create_user(username="allowed_user")
        Profile.objects.create(user=allowed_user, language=self.language)
        UsernameModerationCase.objects.create(
            user=allowed_user,
            username=allowed_user.username,
            status=UsernameModerationCase.STATUS_REVIEWED,
            decision=UsernameModerationCase.DECISION_ALLOW,
            category=UsernameModerationCase.CATEGORY_SAFE,
        )
        self.client.login(username="admin", password="pw")

        response = self.client.get(reverse("internal_username_moderation"))

        self.assertContains(response, "case_user")
        self.assertContains(response, "Needs review")
        self.assertNotContains(response, "allowed_user")
        self.assertContains(response, "username-action-column")
        self.assertNotContains(response, "Reviewed")

    def test_disable_action_disables_and_hides_identity(self):
        self.client.login(username="admin", password="pw")

        response = self.client.post(
            reverse("internal_username_moderation"),
            {"case": self.case.id, "action": "disable"},
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.case.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.case.public_identity_hidden)
        self.assertEqual(self.case.moderator, self.admin_profile)

    def test_allow_action_reviews_and_unhides_identity(self):
        self.case.public_identity_hidden = True
        self.case.save(update_fields=["public_identity_hidden", "updated_at"])
        self.client.login(username="admin", password="pw")

        response = self.client.post(
            reverse("internal_username_moderation"),
            {"case": self.case.id, "action": "allow"},
        )

        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, UsernameModerationCase.STATUS_REVIEWED)
        self.assertEqual(self.case.decision, UsernameModerationCase.DECISION_ALLOW)
        self.assertFalse(self.case.public_identity_hidden)
        self.assertEqual(self.case.moderator, self.admin_profile)

    def test_hide_and_unhide_actions_toggle_public_identity(self):
        self.client.login(username="admin", password="pw")

        response = self.client.post(
            reverse("internal_username_moderation"),
            {"case": self.case.id, "action": "hide"},
        )

        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertTrue(self.case.public_identity_hidden)
        self.assertEqual(self.case.moderator, self.admin_profile)

        response = self.client.post(
            reverse("internal_username_moderation"),
            {"case": self.case.id, "action": "unhide"},
        )

        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertFalse(self.case.public_identity_hidden)


class UsernameModerationAuditCommandTest(TestCase):
    @patch(
        "judge.management.commands.audit_username_moderation.moderate_username_task.delay"
    )
    def test_apply_creates_pending_case_and_queues_ai_task(self, delay):
        user = User.objects.create_user(username="regular_user")
        out = StringIO()

        call_command(
            "audit_username_moderation", "--apply", "--limit", "10", stdout=out
        )

        case = UsernameModerationCase.objects.get(user=user)
        self.assertEqual(case.source, UsernameModerationCase.SOURCE_AUDIT)
        self.assertEqual(case.decision, UsernameModerationCase.DECISION_PENDING)
        self.assertEqual(case.category, UsernameModerationCase.CATEGORY_OTHER)
        self.assertIsNone(case.confidence)
        delay.assert_called_once_with(case.id, delete_safe_case=True)
        self.assertIn("Created 1 case(s); queued 1 AI task(s)", out.getvalue())

    @patch(
        "judge.management.commands.audit_username_moderation.moderate_username_task.delay"
    )
    def test_dry_run_does_not_create_cases_or_queue_tasks(self, delay):
        User.objects.create_user(username="regular_user")
        out = StringIO()

        call_command("audit_username_moderation", "--limit", "10", stdout=out)

        self.assertEqual(UsernameModerationCase.objects.count(), 0)
        delay.assert_not_called()
        self.assertIn("Found 1 AI username moderation candidate(s)", out.getvalue())

    def test_active_and_inactive_filters_are_mutually_exclusive(self):
        with self.assertRaises(CommandError):
            call_command(
                "audit_username_moderation",
                "--active-only",
                "--inactive-only",
                stdout=StringIO(),
            )


@override_settings(LANGUAGE_CODE="en")
class InternalRequestTimeTest(TestCase):
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

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="request_time_admin", email="admin@example.com", password="pw"
        )
        Profile.objects.create(user=self.admin, language=self.language)

    @patch("judge.views.internal.logging.getLogger")
    def test_request_time_handles_missing_log_handler(self, get_logger):
        get_logger.return_value.handlers = []
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(reverse("internal_request_time"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "URL Name")

    @patch("judge.views.internal.logging.getLogger")
    def test_request_time_ignores_invalid_sort_key_without_log_handler(
        self, get_logger
    ):
        get_logger.return_value.handlers = []
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(reverse("internal_request_time") + "?order=bad")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "URL Name")
