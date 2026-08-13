from importlib import import_module
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils.module_loading import import_string

from judge.tasks import maintenance
from judge.tasks.chat_moderation import moderate_recent_chat
from judge.tasks.comment_moderation import moderate_recent_comments
from judge.tasks.periodic import run_locked_command
from judge.tasks.post_moderation import moderate_pending_posts


class PeriodicCommandLockTest(SimpleTestCase):
    def test_run_locked_command_calls_command_and_releases_lock(self):
        lock_key = "test:periodic-lock:release"
        cache.delete(lock_key)

        with patch("judge.tasks.periodic.call_command") as call_command:
            result = run_locked_command(
                lock_key,
                "auto_moderate",
                "--chat-only",
                lock_timeout=60,
            )

        self.assertEqual(result, {"success": True})
        call_command.assert_called_once_with("auto_moderate", "--chat-only")
        self.assertIsNone(cache.get(lock_key))

    def test_run_locked_command_skips_when_lock_is_held(self):
        lock_key = "test:periodic-lock:held"
        cache.set(lock_key, "existing", 60)
        self.addCleanup(cache.delete, lock_key)

        with patch("judge.tasks.periodic.call_command") as call_command:
            result = run_locked_command(lock_key, "auto_moderate", lock_timeout=60)

        self.assertEqual(result, {"skipped": True, "reason": "locked"})
        call_command.assert_not_called()


class PeriodicModerationTaskTest(SimpleTestCase):
    @override_settings(
        AUTO_MODERATE_CHAT_ENABLED=True,
        AUTO_MODERATE_CHAT_BATCH_SIZE=7,
        AUTO_MODERATE_CHAT_WINDOW_MINUTES=33,
        AUTO_MODERATE_CHAT_LOCK_TIMEOUT=44,
    )
    @patch("judge.tasks.chat_moderation.run_locked_command")
    def test_chat_moderation_task_passes_expected_command(self, run_locked_command):
        run_locked_command.return_value = {"success": True}

        result = moderate_recent_chat()

        self.assertEqual(result, {"success": True})
        run_locked_command.assert_called_once_with(
            "periodic:auto_moderate_chat",
            "auto_moderate",
            "--chat-only",
            "--batch-size",
            "7",
            "--chat-window-minutes",
            "33",
            lock_timeout=44,
        )

    @override_settings(AUTO_MODERATE_CHAT_ENABLED=False)
    @patch("judge.tasks.chat_moderation.run_locked_command")
    def test_disabled_chat_moderation_task_does_not_dispatch(self, run_locked_command):
        result = moderate_recent_chat()

        self.assertEqual(result, {"skipped": True, "reason": "disabled"})
        run_locked_command.assert_not_called()

    @override_settings(
        AUTO_MODERATE_COMMENTS_ENABLED=True,
        AUTO_MODERATE_COMMENTS_BATCH_SIZE=8,
        AUTO_MODERATE_COMMENTS_WINDOW_MINUTES=55,
        AUTO_MODERATE_COMMENTS_LOCK_TIMEOUT=66,
    )
    @patch("judge.tasks.comment_moderation.run_locked_command")
    def test_comment_moderation_task_passes_expected_command(self, run_locked_command):
        run_locked_command.return_value = {"success": True}

        result = moderate_recent_comments()

        self.assertEqual(result, {"success": True})
        run_locked_command.assert_called_once_with(
            "periodic:auto_moderate_comments",
            "auto_moderate",
            "--comments-only",
            "--batch-size",
            "8",
            "--comment-window-minutes",
            "55",
            lock_timeout=66,
        )

    @override_settings(
        AUTO_MODERATE_POSTS_ENABLED=True,
        AUTO_MODERATE_POSTS_BATCH_SIZE=9,
        AUTO_MODERATE_POSTS_LOCK_TIMEOUT=77,
    )
    @patch("judge.tasks.post_moderation.run_locked_command")
    def test_post_moderation_task_passes_expected_command(self, run_locked_command):
        run_locked_command.return_value = {"success": True}

        result = moderate_pending_posts()

        self.assertEqual(result, {"success": True})
        run_locked_command.assert_called_once_with(
            "periodic:auto_moderate_posts",
            "auto_moderate",
            "--posts-only",
            "--batch-size",
            "9",
            lock_timeout=77,
        )


class PeriodicMaintenanceTaskTest(SimpleTestCase):
    def test_maintenance_tasks_pass_expected_commands(self):
        cases = [
            (
                maintenance.cleanup_inactive_accounts,
                {
                    "PERIODIC_CLEANUP_INACTIVE_ENABLED": True,
                    "PERIODIC_CLEANUP_INACTIVE_BATCH_SIZE": 11,
                    "PERIODIC_CLEANUP_INACTIVE_LOCK_TIMEOUT": 111,
                },
                (
                    "periodic:cleanup_inactive_accounts",
                    "cleanup_inactive",
                    "--users",
                    "--orgs",
                    "--batch-size",
                    "11",
                ),
                {"lock_timeout": 111},
            ),
            (
                maintenance.delete_old_notifications,
                {
                    "PERIODIC_DELETE_OLD_NOTIFICATIONS_ENABLED": True,
                    "PERIODIC_DELETE_OLD_NOTIFICATIONS_BATCH_SIZE": 12,
                    "PERIODIC_DELETE_OLD_NOTIFICATIONS_LOCK_TIMEOUT": 222,
                },
                (
                    "periodic:delete_old_notifications",
                    "delete_old_notifications",
                    "--batch-size",
                    "12",
                ),
                {"lock_timeout": 222},
            ),
            (
                maintenance.delete_old_request_metrics,
                {
                    "REQUEST_METRICS_RETENTION_DAYS": 9,
                    "PERIODIC_DELETE_OLD_REQUEST_METRICS_ENABLED": True,
                    "PERIODIC_DELETE_OLD_REQUEST_METRICS_BATCH_SIZE": 14,
                    "PERIODIC_DELETE_OLD_REQUEST_METRICS_LOCK_TIMEOUT": 244,
                },
                (
                    "periodic:delete_old_request_metrics",
                    "delete_old_request_metrics",
                    "--days",
                    "9",
                    "--batch-size",
                    "14",
                ),
                {"lock_timeout": 244},
            ),
            (
                maintenance.clear_expired_sessions,
                {
                    "PERIODIC_CLEAR_EXPIRED_SESSIONS_ENABLED": True,
                    "PERIODIC_CLEAR_EXPIRED_SESSIONS_BATCH_SIZE": 13,
                    "PERIODIC_CLEAR_EXPIRED_SESSIONS_SLEEP": 1.25,
                    "PERIODIC_CLEAR_EXPIRED_SESSIONS_LOCK_TIMEOUT": 333,
                },
                (
                    "periodic:clear_expired_sessions",
                    "batch_clearsessions",
                    "--batch-size",
                    "13",
                    "--sleep",
                    "1.25",
                ),
                {"lock_timeout": 333},
            ),
            (
                maintenance.recompute_comment_scores,
                {
                    "PERIODIC_RECOMPUTE_COMMENT_SCORES_ENABLED": True,
                    "PERIODIC_RECOMPUTE_COMMENT_SCORES_LOCK_TIMEOUT": 444,
                },
                ("periodic:recompute_comment_scores", "recompute_comment_scores"),
                {"lock_timeout": 444},
            ),
            (
                maintenance.recompute_contributions,
                {
                    "PERIODIC_RECOMPUTE_CONTRIBUTIONS_ENABLED": True,
                    "PERIODIC_RECOMPUTE_CONTRIBUTIONS_LOCK_TIMEOUT": 555,
                },
                ("periodic:recompute_contributions", "recompute_contributions"),
                {"lock_timeout": 555},
            ),
            (
                maintenance.sync_organization_private_flags,
                {
                    "PERIODIC_FIX_ORGANIZATION_PRIVATE_ENABLED": True,
                    "PERIODIC_FIX_ORGANIZATION_PRIVATE_LOCK_TIMEOUT": 666,
                },
                ("periodic:fix_organization_private", "fix_organization_private"),
                {"lock_timeout": 666},
            ),
        ]

        for task, overrides, expected_args, expected_kwargs in cases:
            with self.subTest(task=task.__name__), self.settings(**overrides):
                with patch("judge.tasks.maintenance.run_locked_command") as locked:
                    locked.return_value = {"success": True}

                    result = task()

                    self.assertEqual(result, {"success": True})
                    locked.assert_called_once_with(
                        *expected_args,
                        **expected_kwargs,
                    )

    @override_settings(PERIODIC_RECOMPUTE_CONTRIBUTIONS_ENABLED=False)
    @patch("judge.tasks.maintenance.run_locked_command")
    def test_disabled_maintenance_task_does_not_dispatch(self, run_locked_command):
        result = maintenance.recompute_contributions()

        self.assertEqual(result, {"skipped": True, "reason": "disabled"})
        run_locked_command.assert_not_called()


class CeleryBeatScheduleTest(SimpleTestCase):
    def test_configured_beat_tasks_are_importable(self):
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            with self.subTest(name=name):
                task_name = entry["task"]
                module_name = task_name.rsplit(".", 1)[0]
                import_module(module_name)
                import_string(task_name)
                self.assertEqual(getattr(import_string(task_name), "name"), task_name)
