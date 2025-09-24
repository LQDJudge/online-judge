from django.db import models
from django.utils.translation import gettext_lazy as _
from django.db.models import CASCADE, F, Q
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from datetime import timedelta

from judge.models import Profile
from judge.caching import cache_wrapper


class NotificationCategory(models.TextChoices):
    """Predefined notification categories for better data integrity"""

    ADD_BLOG = "add_blog", _("Added a post")
    ADDED_TO_GROUP = "added_to_group", _("You are added to a group")
    COMMENT = "comment", _("You have a new comment")
    DELETE_BLOG = "delete_blog", _("Deleted a post")
    REJECT_BLOG = "reject_blog", _("Rejected a post")
    APPROVE_BLOG = "approve_blog", _("Approved a post")
    EDIT_BLOG = "edit_blog", _("Edited a post")
    MENTION = "mention", _("Mentioned you")
    ORGANIZATION = "organization", _("Organization")
    PROBLEM = "problem", _("Problem")
    REPLY = "reply", _("Replied you")
    TICKET = "ticket", _("Ticket")
    PROBLEM_PUBLIC = "problem_public", _("Problem visibility changed")
    PROBLEM_PRIVATE = "problem_private", _("Problem visibility changed")


class NotificationManager(models.Manager):
    """Custom manager for Notification model with bulk operations"""

    def create_notification(
        self,
        owner,
        category,
        html_link="",
        author=None,
        extra_data=None,
        deduplicate=True,
    ):
        """Create a single notification with proper validation and automatic deduplication"""
        if deduplicate:
            # Check for similar recent notifications to merge
            similar_notification = self._find_similar_notification(
                owner, category, html_link, author
            )
            if similar_notification:
                # Update the existing notification timestamp and mark as unread
                similar_notification.time = timezone.now()
                if similar_notification.is_read:
                    # If it was read, mark as unread and increment count
                    similar_notification.is_read = False
                    similar_notification.read_at = None
                    similar_notification.save(
                        update_fields=["time", "is_read", "read_at"]
                    )

                    # Update unread count since we're making a read notification unread again
                    NotificationProfile.objects.get_or_create(user=owner)
                    NotificationProfile.objects.filter(user=owner).update(
                        unread_count=F("unread_count") + 1
                    )
                    unseen_notifications_count.dirty(owner)
                else:
                    # Just update the timestamp
                    similar_notification.save(update_fields=["time"])

                return similar_notification

        # Create new notification
        notification = self.create(
            owner=owner,
            category=category,
            html_link=html_link,
            author=author,
            extra_data=extra_data or {},
        )

        # Update unread count
        NotificationProfile.objects.get_or_create(user=owner)
        NotificationProfile.objects.filter(user=owner).update(
            unread_count=F("unread_count") + 1
        )
        unseen_notifications_count.dirty(owner)

        return notification

    def bulk_create_notifications(
        self,
        user_ids,
        category,
        html_link="",
        author=None,
        extra_data=None,
        deduplicate=True,
    ):
        """Create notifications for multiple users efficiently with automatic deduplication"""
        user_ids = list(set(user_ids))
        to_users = Profile.get_cached_instances(*user_ids)

        notifications = []
        profile_updates = []

        for user in to_users:
            if author and user.id == author.id:
                continue

            if deduplicate:
                # Check for similar recent notifications to merge
                similar_notification = self._find_similar_notification(
                    user, category, html_link, author
                )
                if similar_notification:
                    # Update existing notification timestamp and mark as unread if needed
                    similar_notification.time = timezone.now()
                    if similar_notification.is_read:
                        # If it was read, mark as unread and we'll increment count
                        similar_notification.is_read = False
                        similar_notification.read_at = None
                        similar_notification.save(
                            update_fields=["time", "is_read", "read_at"]
                        )
                        profile_updates.append(user)  # Need to update count
                    else:
                        # Just update the timestamp
                        similar_notification.save(update_fields=["time"])
                    continue

            # Create new notification
            notifications.append(
                Notification(
                    owner=user,
                    category=category,
                    html_link=html_link,
                    author=author,
                    extra_data=extra_data or {},
                )
            )
            profile_updates.append(user)

        # Bulk create new notifications
        if notifications:
            self.bulk_create(notifications)

        # Update unread counts for users who got new notifications or reactivated ones
        for user in profile_updates:
            NotificationProfile.objects.get_or_create(user=user)
            NotificationProfile.objects.filter(user=user).update(
                unread_count=F("unread_count") + 1
            )
            unseen_notifications_count.dirty(user)

    def mark_as_read(self, user, notification_ids=None):
        """Mark notifications as read for a user"""
        queryset = self.filter(owner=user, is_read=False)
        if notification_ids:
            queryset = queryset.filter(id__in=notification_ids)

        count = queryset.update(is_read=True, read_at=timezone.now())

        if count > 0:
            # Update unread count
            NotificationProfile.objects.filter(user=user).update(
                unread_count=F("unread_count") - count
            )
            unseen_notifications_count.dirty(user)

        return count

    def delete_old_notifications(self, days=30):
        """Delete notifications older than specified days"""
        cutoff_date = timezone.now() - timedelta(days=days)
        old_notifications = self.filter(time__lt=cutoff_date)

        # Get affected users for cache invalidation
        affected_users = set(old_notifications.values_list("owner_id", flat=True))

        count, _ = old_notifications.delete()

        # Recalculate unread counts for affected users
        for user_id in affected_users:
            try:
                profile = Profile.objects.get(id=user_id)
                actual_count = self.filter(owner=profile, is_read=False).count()
                NotificationProfile.objects.update_or_create(
                    user=profile, defaults={"unread_count": actual_count}
                )
                unseen_notifications_count.dirty(profile)
            except Profile.DoesNotExist:
                continue

        return count

    def _find_similar_notification(self, owner, category, html_link, author):
        """Find similar notification for automatic merging"""
        # Look for notifications from the same author with same category and link
        # within the last 7 days (both read and unread for better merging)
        cutoff_time = timezone.now() - timedelta(days=7)

        similar_notifications = self.filter(
            owner=owner,
            category=category,
            html_link=html_link,
            author=author,
            time__gte=cutoff_time,
        ).order_by("-time")

        return similar_notifications.first()

    def get_filtered_notifications(
        self, owner, category=None, is_read=None, author=None, search=None
    ):
        """Get filtered notifications for a user"""
        queryset = self.filter(owner=owner)

        if category:
            queryset = queryset.filter(category=category)

        if is_read is not None:
            queryset = queryset.filter(is_read=is_read)

        if author:
            queryset = queryset.filter(author=author)

        if search:
            queryset = queryset.filter(
                Q(html_link__icontains=search)
                | Q(author__user__username__icontains=search)
            )

        return queryset.order_by("-time")

    def deduplicate_notifications(self, owner, dry_run=False):
        """Deduplicate similar notifications for a user"""
        # Group notifications by category, author, and html_link
        duplicates_removed = 0

        # Get all unread notifications grouped by similarity criteria
        notifications = self.filter(owner=owner, is_read=False).order_by(
            "category", "author", "html_link", "-time"
        )

        current_group = None
        group_notifications = []

        for notification in notifications:
            group_key = (
                notification.category,
                notification.author_id,
                notification.html_link,
            )

            if current_group != group_key:
                # Process previous group
                if len(group_notifications) > 1:
                    duplicates_removed += self._merge_notification_group(
                        group_notifications, dry_run
                    )

                # Start new group
                current_group = group_key
                group_notifications = [notification]
            else:
                group_notifications.append(notification)

        # Process final group
        if len(group_notifications) > 1:
            duplicates_removed += self._merge_notification_group(
                group_notifications, dry_run
            )

        return duplicates_removed

    def _merge_notification_group(self, notifications, dry_run=False):
        """Merge a group of similar notifications, keeping only the latest"""
        if len(notifications) <= 1:
            return 0

        # Keep the most recent notification
        latest_notification = notifications[0]  # Already ordered by -time
        duplicates_to_remove = notifications[1:]

        if not dry_run:
            # Delete the duplicate notifications
            duplicate_ids = [n.id for n in duplicates_to_remove]
            self.filter(id__in=duplicate_ids).delete()

            # Update unread count
            count_reduction = len(duplicates_to_remove)
            NotificationProfile.objects.filter(user=latest_notification.owner).update(
                unread_count=F("unread_count") - count_reduction
            )
            unseen_notifications_count.dirty(latest_notification.owner)

        return len(duplicates_to_remove)


class Notification(models.Model):
    """Improved notification model with better structure and performance"""

    owner = models.ForeignKey(
        Profile,
        verbose_name=_("owner"),
        related_name="notifications",
        on_delete=CASCADE,
        db_index=True,
    )
    time = models.DateTimeField(
        verbose_name=_("posted time"), auto_now_add=True, db_index=True
    )
    category = models.CharField(
        verbose_name=_("category"),
        max_length=50,
        choices=NotificationCategory.choices,
        db_index=True,
    )
    html_link = models.TextField(
        default="",
        verbose_name=_("html link to comments, used for non-comments"),
        max_length=1000,
    )
    author = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        verbose_name=_("who triggered, used for non-comment"),
        on_delete=CASCADE,
        related_name="authored_notifications",
    )
    is_read = models.BooleanField(
        default=False,
        verbose_name=_("is read"),
        db_index=True,
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("read at"),
    )
    extra_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("extra data"),
        help_text=_("Additional data for complex notifications"),
    )

    objects = NotificationManager()

    class Meta:
        ordering = ["-time"]
        indexes = [
            models.Index(fields=["owner", "is_read"]),
            models.Index(fields=["category", "-time"]),
            models.Index(fields=["time"]),  # For cleanup operations
        ]
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")

    def __str__(self):
        return f"{self.owner.get_username()} - {self.get_category_display()}"

    def verbose_activity(self):
        """Get human-readable activity description"""
        if self.category in [
            NotificationCategory.PROBLEM_PUBLIC,
            NotificationCategory.PROBLEM_PRIVATE,
        ]:
            return self._get_problem_visibility_message()

        return self.get_category_display()

    def _get_problem_visibility_message(self):
        """Handle problem visibility notification messages"""
        is_public = self.category == NotificationCategory.PROBLEM_PUBLIC
        groups = self.extra_data.get("groups", "")

        if groups:
            if is_public:
                return _("The problem is public to: ") + groups
            else:
                return _("The problem is private to: ") + groups
        else:
            return (
                _("The problem is public to everyone.")
                if is_public
                else _("The problem is private.")
            )

    def mark_as_read(self):
        """Mark this notification as read"""
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=["is_read", "read_at"])

            # Update unread count
            NotificationProfile.objects.filter(user=self.owner).update(
                unread_count=F("unread_count") - 1
            )
            unseen_notifications_count.dirty(self.owner)


class NotificationProfile(models.Model):
    """Profile for tracking notification statistics"""

    unread_count = models.IntegerField(default=0, db_index=True)
    user = models.OneToOneField(
        Profile, on_delete=CASCADE, related_name="notification_profile"
    )
    last_read_time = models.DateTimeField(
        null=True, blank=True, verbose_name=_("last read time")
    )

    class Meta:
        verbose_name = _("notification profile")
        verbose_name_plural = _("notification profiles")

    def __str__(self):
        return f"{self.user.get_username()} - {self.unread_count} unread"

    def reset_unread_count(self):
        """Recalculate unread count from actual notifications"""
        actual_count = Notification.objects.filter(
            owner=self.user, is_read=False
        ).count()

        if self.unread_count != actual_count:
            self.unread_count = actual_count
            self.save(update_fields=["unread_count"])
            unseen_notifications_count.dirty(self.user)

        return actual_count


@cache_wrapper(prefix="unc", expected_type=int)
def unseen_notifications_count(profile):
    """Get unseen notifications count for a profile"""
    try:
        return NotificationProfile.objects.get(user=profile).unread_count
    except ObjectDoesNotExist:
        return 0
