from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.admin.profile import UserAdmin
import judge.models.profile as profile_models
from judge.models import (
    Language,
    Profile,
    ProfileModerationCase,
    RequestMetric,
    UsernameModerationCase,
)
from judge.models.profile import get_profile_public_identity
from judge.tasks.username_moderation import (
    moderate_profile_case_task,
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
    def test_block_disables_and_hides_user_regardless_of_confidence(self, call_llm):
        call_llm.return_value = (
            '{"decision":"block","category":"gambling","confidence":0.82,'
            '"reason":"Likely gambling username"}'
        )
        user = User.objects.create_user(username="maybe_badname")
        Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(user=user, username=user.username)

        moderate_username_task(case.id)

        user.refresh_from_db()
        case.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertTrue(case.public_identity_hidden)
        self.assertEqual(case.status, UsernameModerationCase.STATUS_REVIEWED)
        self.assertEqual(case.decision, UsernameModerationCase.DECISION_BLOCK)

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

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_about_moderation_uses_profile_prompt(self, call_llm):
        call_llm.return_value = (
            '{"decision":"allow","category":"safe","confidence":0.94,'
            '"reason":"Normal profile"}'
        )
        user = User.objects.create_user(username="about_safe_user")
        Profile.objects.create(
            user=user,
            language=self.language,
            about="I like Python and share my GitHub projects.",
        )
        case = ProfileModerationCase.objects.create(
            user=user,
            target=ProfileModerationCase.TARGET_ABOUT,
            username=user.username,
            value_snapshot="I like Python and share my GitHub projects.",
            source=ProfileModerationCase.SOURCE_PROFILE_EDIT,
        )

        result = moderate_profile_case_task(case.id)

        self.assertEqual(result["decision"], ProfileModerationCase.DECISION_ALLOW)
        prompt = call_llm.call_args.kwargs["system_prompt"]
        self.assertIn("Default to allow", prompt)
        self.assertIn("public self-description", prompt)

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_about_block_hides_identity_without_disabling_user(self, call_llm):
        call_llm.return_value = (
            '{"decision":"block","category":"spam","confidence":0.96,'
            '"reason":"Scam profile text"}'
        )
        user = User.objects.create_user(username="about_block_user", is_active=True)
        Profile.objects.create(
            user=user,
            language=self.language,
            about="Click this fake support login to win prizes",
        )
        case = ProfileModerationCase.objects.create(
            user=user,
            target=ProfileModerationCase.TARGET_ABOUT,
            username=user.username,
            value_snapshot="Click this fake support login to win prizes",
            source=ProfileModerationCase.SOURCE_PROFILE_EDIT,
        )

        moderate_profile_case_task(case.id)

        user.refresh_from_db()
        case.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(case.public_identity_hidden)
        self.assertEqual(case.status, ProfileModerationCase.STATUS_REVIEWED)
        self.assertEqual(case.decision, ProfileModerationCase.DECISION_BLOCK)

    @override_settings(POE_API_KEY="test-key")
    @patch("judge.tasks.username_moderation.LLMService.call_llm")
    def test_stale_about_case_does_not_hide_identity(self, call_llm):
        user = User.objects.create_user(username="about_stale_user", is_active=True)
        profile = Profile.objects.create(
            user=user,
            language=self.language,
            about="Fixed safe profile text",
        )
        case = ProfileModerationCase.objects.create(
            user=user,
            target=ProfileModerationCase.TARGET_ABOUT,
            username=user.username,
            value_snapshot="Old unsafe profile text",
            source=ProfileModerationCase.SOURCE_PROFILE_EDIT,
        )

        result = moderate_profile_case_task(case.id)

        call_llm.assert_not_called()
        case.refresh_from_db()
        self.assertEqual(
            result, {"skipped": True, "reason": "stale profile self-description"}
        )
        self.assertEqual(case.status, ProfileModerationCase.STATUS_REVIEWED)
        self.assertEqual(case.decision, ProfileModerationCase.DECISION_ALLOW)
        self.assertFalse(case.public_identity_hidden)
        self.assertEqual(profile.get_public_username(), user.username)


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

    @patch("judge.views.user.moderate_profile_case_task.delay")
    def test_profile_edit_creates_about_moderation_case(self, delay):
        user = User.objects.create_user(username="profile_edit_user", password="pw")
        profile = Profile.objects.create(user=user, language=self.language)
        self.client.login(username="profile_edit_user", password="pw")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("user_edit_profile"),
                {
                    "first_name": "",
                    "last_name": "",
                    "about": "Visit my GitHub for programming projects.",
                    "timezone": profile.timezone,
                    "language": self.language.id,
                    "ace_theme": profile.ace_theme,
                    "profile_image": "",
                    "background_image": "",
                    "tshirt_size": "",
                    "date_of_birth": "",
                    "address": "",
                },
            )

        self.assertEqual(response.status_code, 302)
        case = ProfileModerationCase.objects.get(user=user)
        self.assertEqual(case.target, ProfileModerationCase.TARGET_ABOUT)
        self.assertEqual(
            case.value_snapshot, "Visit my GitHub for programming projects."
        )
        self.assertEqual(case.source, ProfileModerationCase.SOURCE_PROFILE_EDIT)
        delay.assert_called_once_with(case.id)

    def test_user_list_renders_references_to_hidden_users(self):
        user = User.objects.create_user(username="hidden_reference_name")
        Profile.objects.create(
            user=user,
            language=self.language,
            about="[user:hidden_reference_name]",
        )
        UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            decision=UsernameModerationCase.DECISION_REVIEW,
            status=UsernameModerationCase.STATUS_PENDING,
            public_identity_hidden=True,
        )

        response = self.client.get(reverse("user_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disabled user")

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


@override_settings(LANGUAGE_CODE="en")
class UsernameModerationUserAdminTest(TestCase):
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
        cls.admin_user = User.objects.create_superuser(
            username="admin", password="pw", email="admin@example.com"
        )
        cls.admin_profile = Profile.objects.create(
            user=cls.admin_user,
            language=cls.language,
        )

    def setUp(self):
        cache.clear()
        self.model_admin = UserAdmin(User, AdminSite())
        self.request = RequestFactory().post("/admin/auth/user/1/change/")
        self.request.user = self.admin_user

    def test_user_admin_form_includes_hide_username_below_active(self):
        user = User.objects.create_user(username="admin_form_user")

        form_class = self.model_admin.get_form(self.request, user)

        permission_fields = self.model_admin.fieldsets[2][1]["fields"]
        self.assertIn("hide_public_identity", form_class.base_fields)
        self.assertEqual(
            permission_fields[:3],
            ("is_active", "hide_public_identity", "is_staff"),
        )

    def test_user_admin_hide_username_reuses_existing_block_case(self):
        user = User.objects.create_user(username="linkzowincom1", is_active=False)
        profile = Profile.objects.create(user=user, language=self.language)
        case = UsernameModerationCase.objects.create(
            user=user,
            username=user.username,
            decision=UsernameModerationCase.DECISION_BLOCK,
            category=UsernameModerationCase.CATEGORY_GAMBLING,
            source=UsernameModerationCase.SOURCE_REGISTRATION,
            public_identity_hidden=False,
            is_automated=True,
        )

        self.model_admin.save_model(
            self.request,
            user,
            SimpleNamespace(cleaned_data={"hide_public_identity": True}),
            change=True,
        )

        case.refresh_from_db()
        self.assertTrue(case.public_identity_hidden)
        self.assertEqual(case.moderator, self.admin_profile)
        self.assertEqual(UsernameModerationCase.objects.filter(user=user).count(), 1)
        self.assertEqual(profile.get_public_username(), "Disabled user")

        self.model_admin.save_model(
            self.request,
            user,
            SimpleNamespace(cleaned_data={"hide_public_identity": False}),
            change=True,
        )

        case.refresh_from_db()
        self.assertFalse(case.public_identity_hidden)
        get_profile_public_identity.dirty(profile.id)
        self.assertEqual(profile.get_public_username(), user.username)

    def test_user_admin_hide_identity_does_not_reuse_about_case(self):
        user = User.objects.create_user(username="admin_about_case_user")
        profile = Profile.objects.create(user=user, language=self.language)
        about_case = ProfileModerationCase.objects.create(
            user=user,
            target=ProfileModerationCase.TARGET_ABOUT,
            username=user.username,
            value_snapshot="Queued profile text",
            source=ProfileModerationCase.SOURCE_PROFILE_EDIT,
            decision=ProfileModerationCase.DECISION_REVIEW,
            category=ProfileModerationCase.CATEGORY_OTHER,
        )

        self.model_admin.save_model(
            self.request,
            user,
            SimpleNamespace(cleaned_data={"hide_public_identity": True}),
            change=True,
        )

        about_case.refresh_from_db()
        identity_case = ProfileModerationCase.objects.get(
            user=user,
            target=ProfileModerationCase.TARGET_USERNAME,
            public_identity_hidden=True,
        )
        self.assertFalse(about_case.public_identity_hidden)
        self.assertEqual(identity_case.source, ProfileModerationCase.SOURCE_MANUAL)
        self.assertEqual(profile.get_public_username(), "Disabled user")

        about_case.allow(moderator=self.admin_profile)

        get_profile_public_identity.dirty(profile.id)
        self.assertEqual(profile.get_public_username(), "Disabled user")


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


class ProfileModerationAuditCommandTest(TestCase):
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

    @patch(
        "judge.management.commands.audit_profile_moderation."
        "moderate_profile_case_task.delay"
    )
    def test_about_target_creates_pending_case_and_queues_ai_task(self, delay):
        user = User.objects.create_user(username="about_audit_user")
        Profile.objects.create(
            user=user,
            language=self.language,
            about="I am learning competitive programming.",
        )
        out = StringIO()

        call_command(
            "audit_profile_moderation",
            "--target",
            "about",
            "--apply",
            "--limit",
            "10",
            stdout=out,
        )

        case = ProfileModerationCase.objects.get(user=user)
        self.assertEqual(case.target, ProfileModerationCase.TARGET_ABOUT)
        self.assertEqual(case.source, ProfileModerationCase.SOURCE_AUDIT)
        self.assertEqual(case.decision, ProfileModerationCase.DECISION_PENDING)
        self.assertEqual(case.value_snapshot, "I am learning competitive programming.")
        delay.assert_called_once_with(case.id, delete_safe_case=True)
        self.assertIn("Created 1 case(s); queued 1 AI task(s)", out.getvalue())


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

    def create_metric(self, **kwargs):
        defaults = {
            "time": timezone.now(),
            "url_name": "problem_detail",
            "path": "/problem/a",
            "full_url": "http://testserver/problem/a",
            "method": "GET",
            "status_code": 200,
            "is_authenticated": False,
            "username": "",
            "response_time_ms": 100,
            "db_query_count": 2,
            "db_time_ms": 20,
            "cache_call_count": 3,
            "cache_time_ms": 4,
            "profiler": {},
        }
        defaults.update(kwargs)
        return RequestMetric.objects.create(**defaults)

    def test_request_time_handles_empty_metrics(self):
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(reverse("internal_request_time"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "URL Name")
        self.assertContains(response, "Links")

    @override_settings(SLOW_REQUEST_THRESHOLD_SECONDS=1)
    def test_request_time_aggregates_recent_metric_data(self):
        now = timezone.now()
        self.create_metric(response_time_ms=100, time=now - timedelta(minutes=3))
        self.create_metric(response_time_ms=200, time=now - timedelta(minutes=2))
        self.create_metric(
            response_time_ms=1000,
            time=now - timedelta(minutes=1),
            profiler={
                "slowest_queries": [{"sql": "SELECT 1", "time_ms": 15, "many": False}]
            },
        )
        self.create_metric(
            url_name="contest_view",
            path="/contest/x",
            full_url="http://testserver/contest/x",
            response_time_ms=300,
            time=now,
        )
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(reverse("internal_request_time"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Slowest")
        pages = {page["url_name"]: page for page in response.context["pages"]}
        self.assertAlmostEqual(pages["problem_detail"]["avg_time"], 433.333, places=2)
        self.assertEqual(pages["problem_detail"]["p95_time"], 1000)
        self.assertEqual(pages["problem_detail"]["max_time"], 1000)
        self.assertEqual(pages["problem_detail"]["slow_count"], 1)
        self.assertEqual(pages["problem_detail"]["profiled_count"], 1)
        self.assertAlmostEqual(pages["problem_detail"]["avg_cache_time"], 4, places=2)
        self.assertAlmostEqual(
            pages["problem_detail"]["avg_cache_call_count"], 3, places=2
        )
        detail_response = self.client.get(
            reverse("internal_request_time_detail") + "?url_name=problem_detail"
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "P95 (ms)")
        self.assertEqual(detail_response.context["route_summary"]["max_time"], 1000)

    def test_request_time_detail_sorts_by_status_code(self):
        self.create_metric(response_time_ms=100, status_code=200)
        self.create_metric(response_time_ms=150, status_code=302, method="POST")
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(
            reverse("internal_request_time_detail")
            + "?url_name=problem_detail&order=status_code"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["requests"][0].status_code, 302)
        self.assertContains(response, "Route stats")

    def test_request_metric_profile_shows_sampled_profiler_data(self):
        metric = self.create_metric(
            profiler={
                "slowest_queries": [{"sql": "SELECT 1", "time_ms": 15, "many": False}],
                "cache": {
                    "calls": 2,
                    "time_ms": 3,
                    "errors": 0,
                    "by_operation": {"get": {"count": 2, "time_ms": 3, "errors": 0}},
                    "slowest_operations": [
                        {"operation": "get", "time_ms": 2, "error": False}
                    ],
                },
            }
        )
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(
            reverse("internal_request_metric_profile", kwargs={"metric_id": metric.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SELECT 1")
        self.assertContains(response, "Cache operations")

    def test_request_time_filters_logged_out_requests(self):
        self.create_metric(
            response_time_ms=100, is_authenticated=True, username="alice"
        )
        self.create_metric(
            url_name="contest_view",
            path="/contest/x",
            full_url="http://testserver/contest/x",
            response_time_ms=300,
            username="",
        )
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(
            reverse("internal_request_time") + "?auth=logged_out"
        )

        self.assertEqual(response.status_code, 200)
        pages = {page["url_name"]: page for page in response.context["pages"]}
        self.assertNotIn("problem_detail", pages)
        self.assertIn("contest_view", pages)

    def test_delete_old_request_metrics_removes_expired_rows(self):
        old_metric = self.create_metric(time=timezone.now() - timedelta(days=10))
        fresh_metric = self.create_metric(time=timezone.now() - timedelta(days=1))

        call_command("delete_old_request_metrics", "--days", "7", stdout=StringIO())

        self.assertFalse(RequestMetric.objects.filter(id=old_metric.id).exists())
        self.assertTrue(RequestMetric.objects.filter(id=fresh_metric.id).exists())

    def test_request_time_ignores_invalid_sort_key(self):
        self.client.login(username="request_time_admin", password="pw")

        response = self.client.get(reverse("internal_request_time") + "?order=bad")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "URL Name")
