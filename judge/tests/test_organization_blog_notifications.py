from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from judge.models import Organization, Profile, Language, BlogPost, Notification


class OrganizationBlogNotificationTestCase(TestCase):
    """Test cases for organization blog post notifications"""

    @classmethod
    def setUpTestData(cls):
        # Create default language required for Profile
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
        # Create admin user
        self.admin_user = User.objects.create_user(
            username="admin_user", password="password123"
        )
        self.admin_profile, _ = Profile.objects.get_or_create(
            user=self.admin_user, defaults={"language": self.language}
        )

        # Create moderator user
        self.mod_user = User.objects.create_user(
            username="mod_user", password="password123"
        )
        self.mod_profile, _ = Profile.objects.get_or_create(
            user=self.mod_user, defaults={"language": self.language}
        )

        # Create second moderator user
        self.mod_user2 = User.objects.create_user(
            username="mod_user2", password="password123"
        )
        self.mod_profile2, _ = Profile.objects.get_or_create(
            user=self.mod_user2, defaults={"language": self.language}
        )

        # Create regular member user
        self.member_user = User.objects.create_user(
            username="member_user", password="password123"
        )
        self.member_profile, _ = Profile.objects.get_or_create(
            user=self.member_user, defaults={"language": self.language}
        )

        # Create organization
        self.organization = Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            short_name="TestOrg",
            about="A test organization",
            registrant=self.admin_profile,
            is_open=True,
        )
        self.organization.admins.add(self.admin_profile)
        self.organization.moderators.add(self.mod_profile)
        self.organization.moderators.add(self.mod_profile2)
        self.organization.members.add(
            self.admin_profile, self.mod_profile, self.mod_profile2, self.member_profile
        )

        self.client = Client()

    def test_admin_receives_notification_on_new_blog(self):
        """Test that admins receive notification when a new blog post is created"""
        self.client.login(username="member_user", password="password123")

        # Clear any existing notifications
        Notification.objects.filter(owner=self.admin_profile).delete()

        # Create a blog post via the view
        response = self.client.post(
            reverse(
                "add_organization_blog",
                args=[self.organization.id, self.organization.slug],
            ),
            {
                "title": "Test Blog Post",
                "content": "This is a test blog post content.",
            },
        )

        # Check that admin received notification
        admin_notifications = Notification.objects.filter(
            owner=self.admin_profile, category="add_blog"
        )
        self.assertEqual(admin_notifications.count(), 1)

    def test_moderators_receive_notification_on_new_blog(self):
        """Test that moderators receive notification when a new blog post is created"""
        self.client.login(username="member_user", password="password123")

        # Clear any existing notifications
        Notification.objects.filter(owner=self.mod_profile).delete()
        Notification.objects.filter(owner=self.mod_profile2).delete()

        # Create a blog post via the view
        response = self.client.post(
            reverse(
                "add_organization_blog",
                args=[self.organization.id, self.organization.slug],
            ),
            {
                "title": "Test Blog Post",
                "content": "This is a test blog post content.",
            },
        )

        # Check that both moderators received notifications
        mod1_notifications = Notification.objects.filter(
            owner=self.mod_profile, category="add_blog"
        )
        mod2_notifications = Notification.objects.filter(
            owner=self.mod_profile2, category="add_blog"
        )
        self.assertEqual(mod1_notifications.count(), 1)
        self.assertEqual(mod2_notifications.count(), 1)

    def test_both_admins_and_moderators_receive_notification(self):
        """Test that both admins and moderators receive notifications"""
        self.client.login(username="member_user", password="password123")

        # Clear any existing notifications
        Notification.objects.all().delete()

        # Create a blog post via the view
        response = self.client.post(
            reverse(
                "add_organization_blog",
                args=[self.organization.id, self.organization.slug],
            ),
            {
                "title": "Test Blog Post",
                "content": "This is a test blog post content.",
            },
        )

        # Check notifications for all admins and moderators
        admin_notifications = Notification.objects.filter(
            owner=self.admin_profile, category="add_blog"
        )
        mod1_notifications = Notification.objects.filter(
            owner=self.mod_profile, category="add_blog"
        )
        mod2_notifications = Notification.objects.filter(
            owner=self.mod_profile2, category="add_blog"
        )

        self.assertEqual(admin_notifications.count(), 1)
        self.assertEqual(mod1_notifications.count(), 1)
        self.assertEqual(mod2_notifications.count(), 1)

        # Regular member should not receive notification
        member_notifications = Notification.objects.filter(
            owner=self.member_profile, category="add_blog"
        )
        self.assertEqual(member_notifications.count(), 0)

    def test_author_does_not_receive_own_notification(self):
        """Test that the post author (who is also a moderator) does not get notified of their own post"""
        # Login as moderator
        self.client.login(username="mod_user", password="password123")

        # Clear any existing notifications
        Notification.objects.all().delete()

        # Create a blog post via the view
        response = self.client.post(
            reverse(
                "add_organization_blog",
                args=[self.organization.id, self.organization.slug],
            ),
            {
                "title": "Test Blog Post by Moderator",
                "content": "This is a test blog post content.",
            },
        )

        # The moderator who created the post should not receive notification for their own post
        # (bulk_create_notifications filters out the author)
        mod1_notifications = Notification.objects.filter(
            owner=self.mod_profile, category="add_blog"
        )
        self.assertEqual(mod1_notifications.count(), 0)

        # But other admins and moderators should still receive it
        admin_notifications = Notification.objects.filter(
            owner=self.admin_profile, category="add_blog"
        )
        mod2_notifications = Notification.objects.filter(
            owner=self.mod_profile2, category="add_blog"
        )
        self.assertEqual(admin_notifications.count(), 1)
        self.assertEqual(mod2_notifications.count(), 1)
