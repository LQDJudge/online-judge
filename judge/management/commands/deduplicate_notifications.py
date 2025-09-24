from django.core.management.base import BaseCommand
from django.utils.translation import gettext as _
from django.db import transaction

from judge.models import Profile, Notification


class Command(BaseCommand):
    help = "Deduplicate similar notifications for users"

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            help="Username to deduplicate notifications for (if not specified, all users)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deduplicated without making changes",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Process users in batches of this size (default: 100)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        username = options.get("user")

        self.stdout.write("Starting notification deduplication...")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        total_duplicates_removed = 0

        if username:
            # Process single user
            try:
                profile = Profile.objects.get(user__username=username)
                duplicates_removed = self._deduplicate_user(profile, dry_run)
                total_duplicates_removed += duplicates_removed

                self.stdout.write(
                    f"User {username}: {duplicates_removed} duplicates {'found' if dry_run else 'removed'}"
                )
            except Profile.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"User '{username}' not found"))
                return
        else:
            # Process all users with notifications
            users_with_notifications = Profile.objects.filter(
                notifications__isnull=False
            ).distinct()

            total_users = users_with_notifications.count()
            self.stdout.write(f"Processing {total_users} users with notifications...")

            processed = 0
            for profile in users_with_notifications.iterator(chunk_size=batch_size):
                duplicates_removed = self._deduplicate_user(profile, dry_run)
                total_duplicates_removed += duplicates_removed
                processed += 1

                if duplicates_removed > 0:
                    self.stdout.write(
                        f"User {profile.username}: {duplicates_removed} duplicates {'found' if dry_run else 'removed'}"
                    )

                if processed % batch_size == 0:
                    self.stdout.write(f"Processed {processed}/{total_users} users...")

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deduplication preview completed! Found {total_duplicates_removed} duplicate notifications."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deduplication completed! Removed {total_duplicates_removed} duplicate notifications."
                )
            )

    def _deduplicate_user(self, profile, dry_run):
        """Deduplicate notifications for a single user"""
        try:
            with transaction.atomic():
                return Notification.objects.deduplicate_notifications(
                    owner=profile, dry_run=dry_run
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Error processing user {profile.username}: {e}")
            )
            return 0
