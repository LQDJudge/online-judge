from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models import Count
from datetime import timedelta

from judge.models import Organization


class Command(BaseCommand):
    help = "Clean up inactive users and organizations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Delete users inactive for more than this many days (default: 7)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )
        parser.add_argument(
            "--users",
            action="store_true",
            help="Clean up inactive users (is_active=False, joined > days ago)",
        )
        parser.add_argument(
            "--orgs",
            action="store_true",
            help="Clean up inactive organizations (no members)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Process items in batches of this size (default: 100)",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cleanup_users = options["users"]
        cleanup_orgs = options["orgs"]
        batch_size = options["batch_size"]

        if not cleanup_users and not cleanup_orgs:
            self.stdout.write(
                self.style.WARNING(
                    "No cleanup target specified. Use --users and/or --orgs"
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("Starting cleanup..."))

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        if cleanup_users:
            self._cleanup_inactive_users(days, dry_run, batch_size)

        if cleanup_orgs:
            self._cleanup_inactive_orgs(dry_run, batch_size)

        self.stdout.write(self.style.SUCCESS("Cleanup completed!"))

    def _cleanup_inactive_users(self, days, dry_run, batch_size):
        """
        Clean up inactive users:
        - is_active=False
        - Joined more than `days` days ago (to give them time to activate)
        - profile__points=0 (no points earned)
        """
        self.stdout.write(
            f"\n=== Cleaning up inactive users (is_active=False, points=0, joined > {days} days ago) ==="
        )

        cutoff_date = timezone.now() - timedelta(days=days)

        # Find inactive users
        inactive_users = User.objects.filter(
            date_joined__lte=cutoff_date,
            is_active=False,
            profile__points=0,
        )

        total_count = inactive_users.count()

        if total_count == 0:
            self.stdout.write("No inactive users found.")
            return

        self.stdout.write(f"Found {total_count} inactive users")

        if dry_run:
            # Show sample of users that would be deleted
            sample_users = inactive_users[:20]
            self.stdout.write("Sample of users that would be deleted:")
            for user in sample_users:
                joined = user.date_joined.strftime("%Y-%m-%d")
                self.stdout.write(f"  {user.username} (joined: {joined})")
            if total_count > 20:
                self.stdout.write(f"  ... and {total_count - 20} more")
            return

        # Delete users
        deleted_total = 0
        for user in inactive_users:
            self.stdout.write(f"  Deleting: {user.username}")
            user.delete()
            deleted_total += 1

        self.stdout.write(
            self.style.SUCCESS(f"Successfully deleted {deleted_total} inactive users")
        )

    def _cleanup_inactive_orgs(self, dry_run, batch_size):
        """
        Clean up inactive organizations:
        - No members
        """
        self.stdout.write("\n=== Cleaning up inactive organizations ===")

        # Find organizations with no members
        inactive_orgs = Organization.objects.annotate(
            member_count=Count("member", distinct=True),
        ).filter(
            member_count=0,
        )

        total_count = inactive_orgs.count()

        if total_count == 0:
            self.stdout.write("No inactive organizations found.")
            return

        self.stdout.write(f"Found {total_count} inactive organizations")

        if dry_run:
            # Show organizations that would be deleted
            for org in inactive_orgs[:20]:
                created = org.creation_date.strftime("%Y-%m-%d")
                self.stdout.write(
                    f"  {org.name} (slug: {org.slug}, created: {created})"
                )
            if total_count > 20:
                self.stdout.write(f"  ... and {total_count - 20} more")
            return

        # Delete organizations
        deleted_total = 0
        while True:
            org_ids = list(inactive_orgs.values_list("id", flat=True)[:batch_size])

            if not org_ids:
                break

            for org_id in org_ids:
                try:
                    org = Organization.objects.get(id=org_id)
                    org_name = org.name
                    self.stdout.write(f"  Deleting: {org_name}")
                    org.delete()
                    deleted_total += 1
                except Organization.DoesNotExist:
                    pass

            self.stdout.write(f"Deleted {deleted_total} organizations so far...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully deleted {deleted_total} inactive organizations"
            )
        )
