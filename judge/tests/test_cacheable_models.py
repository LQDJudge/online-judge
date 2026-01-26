from django.test import TestCase
from django.contrib.auth.models import User
from django.core.cache import cache

from judge.models import (
    Organization,
    Profile,
    Language,
    Problem,
    ProblemGroup,
)
from judge.models.profile import (
    OrganizationProfile,
    _get_most_recent_organization_ids,
    _get_organization,
)


class CacheableModelTestCase(TestCase):
    """Test cases for CacheableModel and get_cached_instances behavior"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3C",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test_cache",
            defaults={"full_name": "Test Cache Group"},
        )

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="test_cache_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    # ==================== Organization Tests ====================

    def test_organization_get_cached_instances_returns_valid_orgs(self):
        """get_cached_instances should return Organization instances for valid IDs"""
        org1 = Organization.objects.create(
            name="Cache Test Org 1",
            slug="cache-test-org-1",
            short_name="CTO1",
            about="Test org 1",
            registrant=self.profile,
            is_open=True,
        )
        org2 = Organization.objects.create(
            name="Cache Test Org 2",
            slug="cache-test-org-2",
            short_name="CTO2",
            about="Test org 2",
            registrant=self.profile,
            is_open=True,
        )

        instances = Organization.get_cached_instances(org1.id, org2.id)

        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].id, org1.id)
        self.assertEqual(instances[1].id, org2.id)

    def test_organization_get_cached_instances_filters_deleted(self):
        """get_cached_instances should filter out deleted organizations"""
        org1 = Organization.objects.create(
            name="Cache Test Org Keep",
            slug="cache-test-org-keep",
            short_name="CTOK",
            about="Test org to keep",
            registrant=self.profile,
            is_open=True,
        )
        org2 = Organization.objects.create(
            name="Cache Test Org Delete",
            slug="cache-test-org-delete",
            short_name="CTOD",
            about="Test org to delete",
            registrant=self.profile,
            is_open=True,
        )

        org1_id = org1.id
        org2_id = org2.id

        # Delete org2
        org2.delete()

        # get_cached_instances should only return org1
        instances = Organization.get_cached_instances(org1_id, org2_id)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, org1_id)

    def test_organization_get_cached_instances_empty_for_all_deleted(self):
        """get_cached_instances should return empty list if all orgs are deleted"""
        org = Organization.objects.create(
            name="Cache Test Org Temp",
            slug="cache-test-org-temp",
            short_name="CTOT",
            about="Temp org",
            registrant=self.profile,
            is_open=True,
        )
        org_id = org.id
        org.delete()

        instances = Organization.get_cached_instances(org_id)

        self.assertEqual(len(instances), 0)

    # ==================== Profile Tests ====================

    def test_profile_get_cached_instances_returns_valid_profiles(self):
        """get_cached_instances should return Profile instances for valid IDs"""
        user2 = User.objects.create_user(
            username="test_cache_user2", password="password123"
        )
        profile2, _ = Profile.objects.get_or_create(
            user=user2, defaults={"language": self.language}
        )

        instances = Profile.get_cached_instances(self.profile.id, profile2.id)

        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].id, self.profile.id)
        self.assertEqual(instances[1].id, profile2.id)

    def test_profile_get_cached_instances_filters_deleted(self):
        """get_cached_instances should filter out deleted profiles"""
        user2 = User.objects.create_user(
            username="test_cache_user_del", password="password123"
        )
        profile2, _ = Profile.objects.get_or_create(
            user=user2, defaults={"language": self.language}
        )

        profile1_id = self.profile.id
        profile2_id = profile2.id

        # Delete profile2 and its user
        profile2.delete()
        user2.delete()

        instances = Profile.get_cached_instances(profile1_id, profile2_id)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, profile1_id)

    # ==================== Problem Tests ====================

    def test_problem_get_cached_instances_returns_valid_problems(self):
        """get_cached_instances should return Problem instances for valid IDs"""
        problem1 = Problem.objects.create(
            code="cache_test_prob1",
            name="Cache Test Problem 1",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )
        problem2 = Problem.objects.create(
            code="cache_test_prob2",
            name="Cache Test Problem 2",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )

        instances = Problem.get_cached_instances(problem1.id, problem2.id)

        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].id, problem1.id)
        self.assertEqual(instances[1].id, problem2.id)

    def test_problem_get_cached_instances_filters_deleted(self):
        """get_cached_instances should filter out deleted problems"""
        problem1 = Problem.objects.create(
            code="cache_test_prob_keep",
            name="Cache Test Problem Keep",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )
        problem2 = Problem.objects.create(
            code="cache_test_prob_del",
            name="Cache Test Problem Delete",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )

        problem1_id = problem1.id
        problem2_id = problem2.id

        problem2.delete()

        instances = Problem.get_cached_instances(problem1_id, problem2_id)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, problem1_id)


class OrganizationCacheInvalidationTestCase(TestCase):
    """Test cases for cache invalidation when organizations are deleted"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3I",
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
        cache.clear()
        self.user = User.objects.create_user(
            username="test_cache_inv_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    def test_org_delete_invalidates_visited_profiles_cache(self):
        """Deleting an org should invalidate cache for profiles that visited it"""
        org = Organization.objects.create(
            name="Cache Inv Test Org",
            slug="cache-inv-test-org",
            short_name="CITO",
            about="Test org for cache invalidation",
            registrant=self.profile,
            is_open=True,
        )

        # Simulate user visiting the org (creates OrganizationProfile)
        OrganizationProfile.add_organization(self.profile, org)

        # Populate the cache
        org_ids = _get_most_recent_organization_ids(self.profile)
        self.assertIn(org.id, org_ids)

        # Delete the organization
        org.delete()

        # Cache should be invalidated - querying again should not include deleted org
        org_ids_after = _get_most_recent_organization_ids(self.profile)
        self.assertEqual(len(org_ids_after), 0)

    def test_org_delete_invalidates_cache_for_non_member_visitor(self):
        """Cache invalidation should work for visitors who are not members"""
        # Create another user who visits but is not a member
        visitor_user = User.objects.create_user(
            username="visitor_user", password="password123"
        )
        visitor_profile, _ = Profile.objects.get_or_create(
            user=visitor_user, defaults={"language": self.language}
        )

        org = Organization.objects.create(
            name="Cache Inv Visitor Org",
            slug="cache-inv-visitor-org",
            short_name="CIVO",
            about="Test org for visitor cache",
            registrant=self.profile,
            is_open=True,
        )

        # Visitor visits the org (not a member, just visiting)
        OrganizationProfile.add_organization(visitor_profile, org)

        # Populate cache for visitor
        org_ids = _get_most_recent_organization_ids(visitor_profile)
        self.assertIn(org.id, org_ids)

        # Delete the organization
        org.delete()

        # Visitor's cache should also be invalidated
        org_ids_after = _get_most_recent_organization_ids(visitor_profile)
        self.assertEqual(len(org_ids_after), 0)


class BatchCachingFallbackTestCase(TestCase):
    """Test cases for batch caching fallback behavior"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3B",
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
        cache.clear()
        self.user = User.objects.create_user(
            username="test_batch_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    def test_batch_handles_mixed_valid_and_invalid_ids(self):
        """batch() should handle a mix of valid and invalid IDs gracefully"""
        org = Organization.objects.create(
            name="Batch Test Org",
            slug="batch-test-org",
            short_name="BTO",
            about="Test org for batch",
            registrant=self.profile,
            is_open=True,
        )

        valid_id = org.id
        invalid_id = 999999  # Non-existent ID

        # Should not raise an exception
        results = _get_organization.batch([(valid_id,), (invalid_id,)])

        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0])  # Valid org
        self.assertIsNone(results[1])  # Invalid org returns None

    def test_batch_all_invalid_ids_returns_all_none(self):
        """batch() should return all None for all invalid IDs"""
        invalid_ids = [999997, 999998, 999999]

        results = _get_organization.batch([(id,) for id in invalid_ids])

        self.assertEqual(len(results), 3)
        self.assertTrue(all(r is None for r in results))

    def test_get_cached_instances_with_stale_cache(self):
        """get_cached_instances should handle stale cache entries gracefully"""
        org1 = Organization.objects.create(
            name="Stale Cache Org 1",
            slug="stale-cache-org-1",
            short_name="SCO1",
            about="Test org 1",
            registrant=self.profile,
            is_open=True,
        )
        org2 = Organization.objects.create(
            name="Stale Cache Org 2",
            slug="stale-cache-org-2",
            short_name="SCO2",
            about="Test org 2",
            registrant=self.profile,
            is_open=True,
        )

        org1_id = org1.id
        org2_id = org2.id

        # Populate cache
        Organization.get_cached_instances(org1_id, org2_id)

        # Delete org2 directly from DB without going through model.delete()
        # This simulates a stale cache scenario
        Organization.objects.filter(id=org2_id).delete()

        # Clear the cache for org2 to simulate partial cache invalidation failure
        _get_organization.dirty(org2_id)

        # Should still work, returning only org1
        instances = Organization.get_cached_instances(org1_id, org2_id)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, org1_id)


class CacheableModelGetterTestCase(TestCase):
    """Test cases for CacheableModel getter methods and attribute access"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3G",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test_getter",
            defaults={"full_name": "Test Getter Group"},
        )

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="getter_test_user",
            email="getter@test.com",
            password="password123",
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    # ==================== Organization Tests ====================

    def test_organization_cached_instance_getter_and_attribute(self):
        """Both get_name() and .name should work on cached Organization instance"""
        org = Organization.objects.create(
            name="Getter Test Org",
            slug="getter-test-org",
            short_name="GTO",
            about="Test org",
            registrant=self.profile,
            is_open=True,
        )

        # Create instance from just ID (simulates get_cached_instances)
        cached_org = Organization(id=org.id)

        # Both getter and attribute should return same value
        self.assertEqual(cached_org.get_name(), "Getter Test Org")
        self.assertEqual(cached_org.name, "Getter Test Org")
        self.assertEqual(cached_org.get_slug(), "getter-test-org")
        self.assertEqual(cached_org.slug, "getter-test-org")
        self.assertEqual(cached_org.get_short_name(), "GTO")
        self.assertEqual(cached_org.short_name, "GTO")

    # ==================== Profile Tests ====================

    def test_profile_cached_instance_getter_and_attribute(self):
        """Both get_username() and .username should work on cached Profile instance"""
        cached_profile = Profile(id=self.profile.id)

        # Both getter and attribute should return same value
        self.assertEqual(cached_profile.get_username(), "getter_test_user")
        self.assertEqual(cached_profile.username, "getter_test_user")
        self.assertEqual(cached_profile.get_email(), "getter@test.com")
        self.assertEqual(cached_profile.email, "getter@test.com")

    # ==================== Problem Tests ====================

    def test_problem_cached_instance_getter_and_attribute(self):
        """Both get_name() and .name should work on cached Problem instance"""
        import uuid

        unique_code = f"getter_{uuid.uuid4().hex[:8]}"
        problem = Problem.objects.create(
            code=unique_code,
            name="Getter Test Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=25.5,
            is_public=True,  # Required to avoid points being capped to 1
        )

        # Dirty cache to ensure fresh data
        Problem.dirty_cache(problem.id)

        cached_problem = Problem(id=problem.id)

        # Both getter and attribute should return same value
        self.assertEqual(cached_problem.get_name(), "Getter Test Problem")
        self.assertEqual(cached_problem.name, "Getter Test Problem")
        self.assertEqual(cached_problem.get_code(), unique_code)
        self.assertEqual(cached_problem.code, unique_code)
        self.assertEqual(cached_problem.get_points(), 25.5)
        self.assertEqual(cached_problem.points, 25.5)

    # ==================== Cache Invalidation Tests ====================

    def test_dirty_cache_invalidates_and_refreshes(self):
        """dirty_cache should invalidate cached values"""
        org = Organization.objects.create(
            name="Dirty Cache Org",
            slug="dirty-cache-org",
            short_name="DCO",
            about="Test org",
            registrant=self.profile,
            is_open=True,
        )

        # Populate cache
        self.assertEqual(Organization(id=org.id).name, "Dirty Cache Org")

        # Update directly in DB (bypassing model.save)
        Organization.objects.filter(id=org.id).update(name="Updated Name")

        # Cache still has old value
        self.assertEqual(Organization(id=org.id).name, "Dirty Cache Org")

        # Dirty the cache
        Organization.dirty_cache(org.id)

        # Now returns new value
        self.assertEqual(Organization(id=org.id).name, "Updated Name")

    def test_save_automatically_dirties_cache(self):
        """Saving a model should automatically invalidate its cache"""
        org = Organization.objects.create(
            name="Auto Dirty Org",
            slug="auto-dirty-org",
            short_name="ADO",
            about="Test org",
            registrant=self.profile,
            is_open=True,
        )

        # Populate cache
        self.assertEqual(Organization(id=org.id).name, "Auto Dirty Org")

        # Update via save() - should auto-dirty
        org.name = "Saved Name"
        org.save()

        # Should return new value
        self.assertEqual(Organization(id=org.id).name, "Saved Name")
