from django.test import TestCase
from django.contrib.auth.models import User

from judge.models import Organization, Profile, Language


class OrganizationCommunityTestCase(TestCase):
    """Test cases for Community (is_community) feature"""

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
        # Create users with language
        self.admin_user = User.objects.create_user(
            username="admin_user", password="password123"
        )
        self.admin_profile, _ = Profile.objects.get_or_create(
            user=self.admin_user, defaults={"language": self.language}
        )

        self.mod_user = User.objects.create_user(
            username="mod_user", password="password123"
        )
        self.mod_profile, _ = Profile.objects.get_or_create(
            user=self.mod_user, defaults={"language": self.language}
        )

        self.regular_user = User.objects.create_user(
            username="regular_user", password="password123"
        )
        self.regular_profile, _ = Profile.objects.get_or_create(
            user=self.regular_user, defaults={"language": self.language}
        )

        # Create a regular organization
        self.regular_org = Organization.objects.create(
            name="Regular Org",
            slug="regular-org",
            short_name="RegOrg",
            about="A regular organization",
            registrant=self.admin_profile,
            is_open=True,
            is_community=False,
        )
        self.regular_org.admins.add(self.admin_profile)

        # Create a community
        self.community = Organization.objects.create(
            name="Test Community",
            slug="test-community",
            short_name="TestCom",
            about="A test community",
            registrant=self.admin_profile,
            is_open=True,
            is_community=True,
        )
        self.community.admins.add(self.admin_profile)
        self.community.moderators.add(self.mod_profile)

    def test_is_community_field_exists(self):
        """Test that is_community field exists and works"""
        self.assertFalse(self.regular_org.is_community)
        self.assertTrue(self.community.is_community)

    def test_moderators_field_exists(self):
        """Test that moderators field exists and works"""
        self.assertEqual(self.regular_org.moderators.count(), 0)
        self.assertEqual(self.community.moderators.count(), 1)
        self.assertIn(self.mod_profile, self.community.moderators.all())

    def test_is_admin(self):
        """Test is_admin method"""
        self.assertTrue(self.community.is_admin(self.admin_profile))
        self.assertFalse(self.community.is_admin(self.mod_profile))
        self.assertFalse(self.community.is_admin(self.regular_profile))

    def test_is_moderator(self):
        """Test is_moderator method"""
        self.assertFalse(self.community.is_moderator(self.admin_profile))
        self.assertTrue(self.community.is_moderator(self.mod_profile))
        self.assertFalse(self.community.is_moderator(self.regular_profile))

    def test_can_moderate_admin(self):
        """Test that admins can moderate"""
        self.assertTrue(self.community.can_moderate(self.admin_profile))

    def test_can_moderate_moderator(self):
        """Test that moderators can moderate"""
        self.assertTrue(self.community.can_moderate(self.mod_profile))

    def test_can_moderate_regular_user(self):
        """Test that regular users cannot moderate"""
        self.assertFalse(self.community.can_moderate(self.regular_profile))

    def test_can_moderate_none(self):
        """Test that None profile cannot moderate"""
        self.assertFalse(self.community.can_moderate(None))

    def test_get_moderator_ids(self):
        """Test get_moderator_ids method"""
        mod_ids = self.community.get_moderator_ids()
        self.assertIn(self.mod_profile.id, mod_ids)
        self.assertNotIn(self.admin_profile.id, mod_ids)

    def test_community_always_open_on_create(self):
        """Test that communities are always open when created"""
        community = Organization.objects.create(
            name="New Community",
            slug="new-community",
            short_name="NewCom",
            about="A new community",
            registrant=self.admin_profile,
            is_open=False,  # Try to create as closed
            is_community=True,
        )
        self.assertTrue(community.is_open)

    def test_community_always_open_on_save(self):
        """Test that communities are always open when saved"""
        # Set is_open to False directly
        self.community.is_open = False
        self.community.save()
        self.community.refresh_from_db()
        # Should still be open because it's a community
        self.assertTrue(self.community.is_open)

    def test_regular_org_can_be_closed(self):
        """Test that regular orgs can be closed"""
        self.regular_org.is_open = False
        self.regular_org.save()
        self.regular_org.refresh_from_db()
        self.assertFalse(self.regular_org.is_open)
