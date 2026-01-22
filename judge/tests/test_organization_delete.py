from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone

from judge.models import (
    Organization,
    Profile,
    Language,
    Problem,
    ProblemGroup,
    Contest,
    BlogPost,
    Course,
)


class OrganizationDeleteTestCase(TestCase):
    """Test cases for Organization.delete() cascading behavior"""

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
            name="test_org_del",
            defaults={"full_name": "Test Group"},
        )

    def setUp(self):
        # Create user
        self.user = User.objects.create_user(
            username="test_user_org_del", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create organizations
        self.org1 = Organization.objects.create(
            name="Test Org Del 1",
            slug="test-org-del-1",
            short_name="TOD1",
            about="A test organization",
            registrant=self.profile,
            is_open=True,
        )
        self.org2 = Organization.objects.create(
            name="Test Org Del 2",
            slug="test-org-del-2",
            short_name="TOD2",
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

    def _create_contest(self, key):
        """Helper to create a contest"""
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=f"Test Contest {key}",
            start_time=now,
            end_time=now + timezone.timedelta(hours=2),
        )

    def _create_blogpost(self, slug):
        """Helper to create a blog post"""
        return BlogPost.objects.create(
            title=f"Test Post {slug}",
            slug=slug,
            content="Test content",
            publish_on=timezone.now(),
        )

    def _create_course(self, slug):
        """Helper to create a course"""
        return Course.objects.create(
            name=f"Test Course {slug}",
            slug=slug,
            about="Test course description",
            is_open=True,
        )

    # ==================== Problem Tests ====================

    def test_delete_org_deletes_problem_with_single_org(self):
        """When an org is deleted, problems with only that org should be deleted"""
        problem = self._create_problem("prob_single_org")
        problem.organizations.add(self.org1)
        problem_id = problem.id

        self.org1.delete()

        self.assertFalse(Problem.objects.filter(id=problem_id).exists())

    def test_delete_org_keeps_problem_with_multiple_orgs(self):
        """When an org is deleted, problems with multiple orgs should remain"""
        problem = self._create_problem("prob_multi_org")
        problem.organizations.add(self.org1, self.org2)
        problem_id = problem.id

        self.org1.delete()

        self.assertTrue(Problem.objects.filter(id=problem_id).exists())
        problem.refresh_from_db()
        self.assertEqual(problem.organizations.count(), 1)
        self.assertIn(self.org2, problem.organizations.all())

    # ==================== Contest Tests ====================

    def test_delete_org_deletes_contest_with_single_org(self):
        """When an org is deleted, contests with only that org should be deleted"""
        contest = self._create_contest("contest_single_org")
        contest.organizations.add(self.org1)
        contest_id = contest.id

        self.org1.delete()

        self.assertFalse(Contest.objects.filter(id=contest_id).exists())

    def test_delete_org_keeps_contest_with_multiple_orgs(self):
        """When an org is deleted, contests with multiple orgs should remain"""
        contest = self._create_contest("contest_multi_org")
        contest.organizations.add(self.org1, self.org2)
        contest_id = contest.id

        self.org1.delete()

        self.assertTrue(Contest.objects.filter(id=contest_id).exists())
        contest.refresh_from_db()
        self.assertEqual(contest.organizations.count(), 1)
        self.assertIn(self.org2, contest.organizations.all())

    # ==================== BlogPost Tests ====================

    def test_delete_org_deletes_blogpost_with_single_org(self):
        """When an org is deleted, blogposts with only that org should be deleted"""
        post = self._create_blogpost("post-single-org")
        post.organizations.add(self.org1)
        post_id = post.id

        self.org1.delete()

        self.assertFalse(BlogPost.objects.filter(id=post_id).exists())

    def test_delete_org_keeps_blogpost_with_multiple_orgs(self):
        """When an org is deleted, blogposts with multiple orgs should remain"""
        post = self._create_blogpost("post-multi-org")
        post.organizations.add(self.org1, self.org2)
        post_id = post.id

        self.org1.delete()

        self.assertTrue(BlogPost.objects.filter(id=post_id).exists())
        post.refresh_from_db()
        self.assertEqual(post.organizations.count(), 1)
        self.assertIn(self.org2, post.organizations.all())

    # ==================== Course Tests ====================

    def test_delete_org_deletes_course_with_single_org(self):
        """When an org is deleted, courses with only that org should be deleted"""
        course = self._create_course("course-single-org")
        course.organizations.add(self.org1)
        course_id = course.id

        self.org1.delete()

        self.assertFalse(Course.objects.filter(id=course_id).exists())

    def test_delete_org_keeps_course_with_multiple_orgs(self):
        """When an org is deleted, courses with multiple orgs should remain"""
        course = self._create_course("course-multi-org")
        course.organizations.add(self.org1, self.org2)
        course_id = course.id

        self.org1.delete()

        self.assertTrue(Course.objects.filter(id=course_id).exists())
        course.refresh_from_db()
        self.assertEqual(course.organizations.count(), 1)
        self.assertIn(self.org2, course.organizations.all())

    # ==================== Combined Tests ====================

    def test_delete_org_cascades_to_all_related_items(self):
        """Test that deleting an org properly cascades to all related item types"""
        problem = self._create_problem("prob_cascade")
        contest = self._create_contest("contest_cascade")
        post = self._create_blogpost("post-cascade")
        course = self._create_course("course-cascade")

        # Add all to org1 only
        problem.organizations.add(self.org1)
        contest.organizations.add(self.org1)
        post.organizations.add(self.org1)
        course.organizations.add(self.org1)

        problem_id = problem.id
        contest_id = contest.id
        post_id = post.id
        course_id = course.id

        # Delete org1
        self.org1.delete()

        # All should be deleted
        self.assertFalse(Problem.objects.filter(id=problem_id).exists())
        self.assertFalse(Contest.objects.filter(id=contest_id).exists())
        self.assertFalse(BlogPost.objects.filter(id=post_id).exists())
        self.assertFalse(Course.objects.filter(id=course_id).exists())

    def test_delete_org_mixed_items_some_deleted_some_kept(self):
        """Test mixed scenario where some items are deleted and some are kept"""
        # Problem with single org (should be deleted)
        problem1 = self._create_problem("prob_mix_1")
        problem1.organizations.add(self.org1)

        # Problem with multiple orgs (should be kept)
        problem2 = self._create_problem("prob_mix_2")
        problem2.organizations.add(self.org1, self.org2)

        # Contest with single org (should be deleted)
        contest1 = self._create_contest("contest_mix_1")
        contest1.organizations.add(self.org1)

        # Contest with multiple orgs (should be kept)
        contest2 = self._create_contest("contest_mix_2")
        contest2.organizations.add(self.org1, self.org2)

        problem1_id = problem1.id
        problem2_id = problem2.id
        contest1_id = contest1.id
        contest2_id = contest2.id

        # Delete org1
        self.org1.delete()

        # Single-org items should be deleted
        self.assertFalse(Problem.objects.filter(id=problem1_id).exists())
        self.assertFalse(Contest.objects.filter(id=contest1_id).exists())

        # Multi-org items should be kept
        self.assertTrue(Problem.objects.filter(id=problem2_id).exists())
        self.assertTrue(Contest.objects.filter(id=contest2_id).exists())
