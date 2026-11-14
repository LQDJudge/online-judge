"""The old lock migration stays inert; the opt-in command respects visibility."""

from datetime import timedelta
from importlib import import_module
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from judge.models import Contest, Problem, ProblemGroup, Quiz


class PublicLockRolloutTests(TestCase):
    def test_old_migration_has_no_automatic_backfill(self):
        migration = import_module("judge.migrations.0279_lock_existing_public_content")
        self.assertEqual(migration.Migration.operations, [])

    def test_explicit_backfill_is_opt_in_idempotent_and_excludes_restricted_content(
        self,
    ):
        group = ProblemGroup.objects.create(
            name="lock-rollout", full_name="Lock rollout"
        )

        def problem(code, **fields):
            return Problem.objects.create(
                code=code,
                name=code,
                group=group,
                time_limit=1,
                memory_limit=65536,
                points=100,
                is_locked=False,
                **fields
            )

        public = problem("lockpublic", is_public=True)
        public2 = problem("lockpublic2", is_public=True)
        private = problem("lockprivate", is_public=False)
        organization = problem("lockorg", is_public=True, is_organization_private=True)
        start = timezone.now()

        def contest(key, **fields):
            return Contest.objects.create(
                key=key,
                name=key,
                start_time=start,
                end_time=start + timedelta(hours=2),
                is_locked=False,
                **fields
            )

        visible = contest("lockvisible", is_visible=True)
        hidden = contest("lockhidden", is_visible=False)
        org_contest = contest(
            "lockorgcontest", is_visible=True, is_organization_private=True
        )
        private_contest = contest(
            "lockprivatecontest", is_visible=True, is_private=True
        )
        course_contest = contest("lockcourse", is_visible=True, is_in_course=True)
        quiz = Quiz.objects.create(
            code="lockquiz", title="Quiz", is_public=True, is_locked=False
        )

        # Simulate existing records created before automatic locking was enabled.
        Problem.objects.filter(pk__in=[public.pk, public2.pk]).update(is_locked=False)
        Contest.objects.filter(pk=visible.pk).update(is_locked=False)
        call_command("lock_site_public_content", batch_size=2, stdout=StringIO())
        for item in (public, public2, visible):
            item.refresh_from_db()
            self.assertFalse(item.is_locked)

        call_command(
            "lock_site_public_content", apply=True, batch_size=2, stdout=StringIO()
        )
        for item in (public, public2, visible):
            item.refresh_from_db()
            self.assertTrue(item.is_locked)
        for item in (
            private,
            organization,
            hidden,
            org_contest,
            private_contest,
            course_contest,
            quiz,
        ):
            item.refresh_from_db()
            self.assertFalse(item.is_locked)
        output = StringIO()
        call_command(
            "lock_site_public_content", apply=True, batch_size=2, stdout=output
        )
        self.assertIn("Locked 0", output.getvalue())
