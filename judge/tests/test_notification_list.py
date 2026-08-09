from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from judge.models import Language, Notification, Profile
from judge.models.notification import NotificationCategory


@override_settings(LANGUAGE_CODE="en")
class NotificationListTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
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
        user = User.objects.create_user("notification_user", password="pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": language}
        )

        for _ in range(3):
            Notification.objects.create_notification(
                owner=cls.profile,
                category=NotificationCategory.COMMENT,
                deduplicate=False,
            )
        for _ in range(2):
            Notification.objects.create_notification(
                owner=cls.profile,
                category=NotificationCategory.MENTION,
                deduplicate=False,
            )
        Notification.objects.create(
            owner=cls.profile,
            category=NotificationCategory.PROBLEM,
            is_read=True,
        )

    def test_categories_show_unseen_counts_sorted_descending(self):
        self.client.force_login(self.profile.user)
        response = self.client.get("/notifications/")

        self.assertEqual(response.status_code, 200)
        categories = response.context["notification_categories"]
        self.assertEqual(len(categories), len(NotificationCategory.choices))
        rendered_categories = [
            (value, str(label), count) for value, label, count in categories
        ]
        self.assertEqual(
            rendered_categories[:2],
            [
                (NotificationCategory.COMMENT, "You have a new comment", 3),
                (NotificationCategory.MENTION, "Mentioned you", 2),
            ],
        )
        counts = {value: count for value, _label, count in categories}
        self.assertEqual(counts[NotificationCategory.PROBLEM], 0)
        self.assertEqual(response.context["unread_notifications"], 5)
        self.assertEqual(response.context["total_notifications"], 6)
        self.assertContains(response, "You have a new comment (3)")
        self.assertContains(response, "Mentioned you (2)")
        self.assertContains(response, "Problem (0)")
