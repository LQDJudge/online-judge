from django.test import TestCase
from django.contrib.auth.models import User

from judge.models import Organization, Profile, Language, Problem, ProblemGroup


class ProblemOrganizationSignalTestCase(TestCase):
    """Test cases for Problem.organizations m2m_changed signal"""

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
        # Create problem group
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test",
            defaults={"full_name": "Test Group"},
        )

    def setUp(self):
        # Create user
        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create organization
        self.org = Organization.objects.create(
            name="Test Org",
            slug="test-org",
            short_name="TestOrg",
            about="A test organization",
            registrant=self.profile,
            is_open=True,
        )

        # Create a second organization
        self.org2 = Organization.objects.create(
            name="Test Org 2",
            slug="test-org-2",
            short_name="TestOrg2",
            about="A second test organization",
            registrant=self.profile,
            is_open=True,
        )

    def _create_problem(self, code):
        """Helper to create a problem"""
        return Problem.objects.create(
            code=code,
            name=f"Test Problem {code}",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )

    def test_is_organization_private_initially_false(self):
        """Test that a new problem has is_organization_private=False"""
        problem = self._create_problem("testprob1")
        self.assertFalse(problem.is_organization_private)
        self.assertEqual(problem.organizations.count(), 0)

    def test_adding_organization_sets_is_organization_private_true(self):
        """Test that adding an organization sets is_organization_private=True"""
        problem = self._create_problem("testprob2")
        self.assertFalse(problem.is_organization_private)

        # Add organization
        problem.organizations.add(self.org)

        # Refresh from database
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)
        self.assertEqual(problem.organizations.count(), 1)

    def test_removing_all_organizations_sets_is_organization_private_false(self):
        """Test that removing all organizations sets is_organization_private=False"""
        problem = self._create_problem("testprob3")

        # Add organization
        problem.organizations.add(self.org)
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)

        # Remove organization
        problem.organizations.remove(self.org)
        problem.refresh_from_db()
        self.assertFalse(problem.is_organization_private)

    def test_clear_organizations_sets_is_organization_private_false(self):
        """Test that clearing organizations sets is_organization_private=False"""
        problem = self._create_problem("testprob4")

        # Add multiple organizations
        problem.organizations.add(self.org, self.org2)
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)
        self.assertEqual(problem.organizations.count(), 2)

        # Clear all organizations
        problem.organizations.clear()
        problem.refresh_from_db()
        self.assertFalse(problem.is_organization_private)

    def test_removing_one_org_keeps_is_organization_private_true(self):
        """Test that removing one org (when multiple exist) keeps is_organization_private=True"""
        problem = self._create_problem("testprob5")

        # Add multiple organizations
        problem.organizations.add(self.org, self.org2)
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)

        # Remove one organization
        problem.organizations.remove(self.org)
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)  # Still True
        self.assertEqual(problem.organizations.count(), 1)

    def test_set_organizations_updates_is_organization_private(self):
        """Test that using .set() updates is_organization_private correctly"""
        problem = self._create_problem("testprob6")

        # Set organizations
        problem.organizations.set([self.org])
        problem.refresh_from_db()
        self.assertTrue(problem.is_organization_private)

        # Set to empty
        problem.organizations.set([])
        problem.refresh_from_db()
        self.assertFalse(problem.is_organization_private)
