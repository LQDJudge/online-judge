from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from copy import deepcopy

from judge.models import (
    Organization,
    Profile,
    Language,
    Contest,
    Course,
)
from judge.models.course import CourseContest


class ContestCloneTestCase(TestCase):
    """Test cases for Contest cloning and is_organization_private behavior"""

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
        # Create user
        self.user = User.objects.create_user(
            username="test_user_clone", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create organizations
        self.org1 = Organization.objects.create(
            name="Test Org Clone 1",
            slug="test-org-clone-1",
            short_name="TOC1",
            about="A test organization",
            registrant=self.profile,
            is_open=True,
        )
        self.org2 = Organization.objects.create(
            name="Test Org Clone 2",
            slug="test-org-clone-2",
            short_name="TOC2",
            about="A second test organization",
            registrant=self.profile,
            is_open=True,
        )

        # Create a course
        self.course = Course.objects.create(
            name="Test Course Clone",
            slug="test-course-clone",
            about="Test course description",
            is_open=True,
        )

    def _create_contest(self, key):
        """Helper to create a contest"""
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=f"Test Contest {key}",
            start_time=now,
            end_time=now + timezone.timedelta(hours=2),
        )

    def test_clone_contest_to_organization_sets_is_organization_private(self):
        """Cloning a contest to an organization should set is_organization_private=True"""
        original = self._create_contest("original_org")
        self.assertFalse(original.is_organization_private)

        # Simulate clone to organization
        cloned = deepcopy(original)
        cloned.pk = None
        cloned.key = "cloned_org"
        cloned.save()
        cloned.organizations.set([self.org1])

        cloned.refresh_from_db()
        self.assertTrue(cloned.is_organization_private)
        self.assertEqual(cloned.organizations.count(), 1)

    def test_clone_contest_to_course_clears_organizations(self):
        """Cloning a contest to a course should clear organizations and set is_organization_private=False"""
        # Create original contest with organizations
        original = self._create_contest("original_course")
        original.organizations.add(self.org1, self.org2)
        original.refresh_from_db()
        self.assertTrue(original.is_organization_private)
        self.assertEqual(original.organizations.count(), 2)

        # Simulate clone to course
        cloned = deepcopy(original)
        cloned.pk = None
        cloned.key = "cloned_course"
        cloned.is_in_course = True
        cloned.save()
        cloned.organizations.clear()  # This is the fix we added

        # Create CourseContest link
        CourseContest.objects.create(
            course=self.course,
            contest=cloned,
            order=1,
            points=0,
        )

        cloned.refresh_from_db()
        self.assertFalse(cloned.is_organization_private)
        self.assertEqual(cloned.organizations.count(), 0)
        self.assertTrue(cloned.is_in_course)

    def test_clone_public_contest_to_course_stays_not_private(self):
        """Cloning a public contest (no orgs) to a course keeps is_organization_private=False"""
        original = self._create_contest("public_to_course")
        self.assertFalse(original.is_organization_private)

        # Simulate clone to course
        cloned = deepcopy(original)
        cloned.pk = None
        cloned.key = "cloned_public_course"
        cloned.is_in_course = True
        cloned.save()
        cloned.organizations.clear()

        CourseContest.objects.create(
            course=self.course,
            contest=cloned,
            order=1,
            points=0,
        )

        cloned.refresh_from_db()
        self.assertFalse(cloned.is_organization_private)
        self.assertEqual(cloned.organizations.count(), 0)

    def test_clone_org_contest_to_different_org(self):
        """Cloning a contest from one org to another should update organizations correctly"""
        # Create original contest with org1
        original = self._create_contest("org1_contest")
        original.organizations.add(self.org1)
        original.refresh_from_db()
        self.assertTrue(original.is_organization_private)

        # Clone to org2
        cloned = deepcopy(original)
        cloned.pk = None
        cloned.key = "cloned_to_org2"
        cloned.save()
        cloned.organizations.set([self.org2])

        cloned.refresh_from_db()
        self.assertTrue(cloned.is_organization_private)
        self.assertEqual(cloned.organizations.count(), 1)
        self.assertIn(self.org2, cloned.organizations.all())
        self.assertNotIn(self.org1, cloned.organizations.all())
