from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from judge.models.notification import (
    Notification,
    NotificationProfile,
    unseen_notifications_count,
)


class Command(BaseCommand):
    help = "Delete old notifications and clean up notification data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete notifications older than this many days (default: 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )
        parser.add_argument(
            "--skip-count-reset",
            action="store_true",
            help="Skip recalculating unread counts (not recommended)",
        )
        parser.add_argument(
            "--cleanup-profiles",
            action="store_true",
            help="Remove notification profiles for users with no notifications",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Process notifications in batches of this size (default: 1000)",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        skip_count_reset = options["skip_count_reset"]
        reset_counts = (
            not skip_count_reset
        )  # Always reset counts unless explicitly skipped
        cleanup_profiles = options["cleanup_profiles"]
        batch_size = options["batch_size"]

        self.stdout.write(self.style.SUCCESS(f"Starting notification cleanup..."))

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        # Delete old notifications
        if days > 0:
            self._delete_old_notifications(days, dry_run, batch_size)

        # Reset unread counts (recommended after deleting notifications)
        if reset_counts:
            self._reset_unread_counts(dry_run)

        # Cleanup notification profiles
        if cleanup_profiles:
            self._cleanup_notification_profiles(dry_run)

        self.stdout.write(self.style.SUCCESS("Notification cleanup completed!"))

    def _delete_old_notifications(self, days, dry_run, batch_size):
        """Delete notifications older than specified days"""
        cutoff_date = timezone.now() - timedelta(days=days)

        self.stdout.write(
            f"Deleting notifications older than {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # Count total notifications to delete
        total_count = Notification.objects.filter(time__lt=cutoff_date).count()

        if total_count == 0:
            self.stdout.write("No old notifications found to delete.")
            return

        self.stdout.write(f"Found {total_count} notifications to delete")

        if dry_run:
            # Show breakdown by category
            from django.db.models import Count

            breakdown = (
                Notification.objects.filter(time__lt=cutoff_date)
                .values("category")
                .annotate(count=Count("id"))
                .order_by("-count")
            )

            self.stdout.write("Breakdown by category:")
            for item in breakdown:
                self.stdout.write(f"  {item['category']}: {item['count']}")
            return

        # Delete in batches to avoid memory issues
        deleted_total = 0
        while True:
            # Get a batch of notification IDs to delete
            notification_ids = list(
                Notification.objects.filter(time__lt=cutoff_date).values_list(
                    "id", flat=True
                )[:batch_size]
            )

            if not notification_ids:
                break

            # Delete the batch
            deleted_count = Notification.objects.filter(
                id__in=notification_ids
            ).delete()[0]

            deleted_total += deleted_count
            self.stdout.write(
                f"Deleted {deleted_count} notifications (total: {deleted_total})"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully deleted {deleted_total} old notifications"
            )
        )

    def _reset_unread_counts(self, dry_run):
        """Recalculate unread counts for all notification profiles"""
        self.stdout.write("Resetting unread counts for all users...")

        # Get all notification profiles
        profiles = NotificationProfile.objects.all()
        total_profiles = profiles.count()

        if total_profiles == 0:
            self.stdout.write("No notification profiles found.")
            return

        self.stdout.write(f"Processing {total_profiles} notification profiles")

        updated_count = 0
        for i, profile in enumerate(profiles, 1):
            # Calculate actual unread count
            actual_count = Notification.objects.filter(
                owner=profile.user, is_read=False
            ).count()

            if profile.unread_count != actual_count:
                if not dry_run:
                    profile.unread_count = actual_count
                    profile.save(update_fields=["unread_count"])
                    # Dirty the cache to ensure UI updates immediately
                    unseen_notifications_count.dirty(profile.user)

                updated_count += 1
                self.stdout.write(
                    f"User {profile.user.get_username()}: {profile.unread_count} -> {actual_count}"
                )

            # Progress indicator
            if i % 100 == 0:
                self.stdout.write(f"Processed {i}/{total_profiles} profiles")

        if dry_run:
            self.stdout.write(f"Would update {updated_count} notification profiles")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Updated {updated_count} notification profiles")
            )

    def _cleanup_notification_profiles(self, dry_run):
        """Remove notification profiles for users with no notifications"""
        self.stdout.write("Cleaning up orphaned notification profiles...")

        # Find profiles where user has no notifications
        from django.db.models import Exists, OuterRef

        orphaned_profiles = NotificationProfile.objects.filter(
            ~Exists(Notification.objects.filter(owner=OuterRef("user")))
        )

        count = orphaned_profiles.count()

        if count == 0:
            self.stdout.write("No orphaned notification profiles found.")
            return

        self.stdout.write(f"Found {count} orphaned notification profiles")

        if dry_run:
            # Show which profiles would be deleted
            for profile in orphaned_profiles[:10]:  # Show first 10
                self.stdout.write(
                    f"  Would delete profile for: {profile.user.get_username()}"
                )
            if count > 10:
                self.stdout.write(f"  ... and {count - 10} more")
        else:
            deleted_count = orphaned_profiles.delete()[0]
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {deleted_count} orphaned notification profiles"
                )
            )

    def _get_statistics(self):
        """Get notification statistics"""
        from django.db.models import Count, Avg
        from django.utils import timezone
        from datetime import timedelta

        total_notifications = Notification.objects.count()
        unread_notifications = Notification.objects.filter(is_read=False).count()

        # Notifications in last 7 days
        week_ago = timezone.now() - timedelta(days=7)
        recent_notifications = Notification.objects.filter(time__gte=week_ago).count()

        # Average notifications per user
        avg_per_user = (
            Notification.objects.aggregate(avg=Avg("owner__notifications__id"))["avg"]
            or 0
        )

        self.stdout.write("\n=== Notification Statistics ===")
        self.stdout.write(f"Total notifications: {total_notifications}")
        self.stdout.write(f"Unread notifications: {unread_notifications}")
        self.stdout.write(f"Recent notifications (7 days): {recent_notifications}")
        self.stdout.write(f"Average per user: {avg_per_user:.2f}")

        # Top categories
        top_categories = (
            Notification.objects.values("category")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )

        self.stdout.write("\nTop notification categories:")
        for cat in top_categories:
            self.stdout.write(f"  {cat['category']}: {cat['count']}")
