from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.utils.problems import user_editable_ids, user_tester_ids


class ProblemEditorCacheInvalidationTest(TestCase):
    """Test that user_editable_ids and user_tester_ids caches
    are invalidated when authors/curators/testers are changed."""

    @classmethod
    def setUpTestData(cls):
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test",
            defaults={"full_name": "Test Group"},
        )

    def setUp(self):
        self.user1 = User.objects.create_user(username="user1", password="pass")
        self.profile1, _ = Profile.objects.get_or_create(
            user=self.user1, defaults={"language": self.language}
        )
        self.user2 = User.objects.create_user(username="user2", password="pass")
        self.profile2, _ = Profile.objects.get_or_create(
            user=self.user2, defaults={"language": self.language}
        )
        self.problem = Problem.objects.create(
            code="testcache",
            name="Test Cache Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )

    def test_add_author_invalidates_cache(self):
        # Populate cache — problem should not be in editable set
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))

        # Add as author
        self.problem.authors.add(self.profile1)

        # Cache should be invalidated — problem now in editable set
        self.assertIn(self.problem.id, user_editable_ids(self.profile1))

    def test_remove_author_invalidates_cache(self):
        self.problem.authors.add(self.profile1)
        self.assertIn(self.problem.id, user_editable_ids(self.profile1))

        self.problem.authors.remove(self.profile1)

        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))

    def test_add_curator_invalidates_cache(self):
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))

        self.problem.curators.add(self.profile1)

        self.assertIn(self.problem.id, user_editable_ids(self.profile1))

    def test_remove_curator_invalidates_cache(self):
        self.problem.curators.add(self.profile1)
        self.assertIn(self.problem.id, user_editable_ids(self.profile1))

        self.problem.curators.remove(self.profile1)

        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))

    def test_add_tester_invalidates_cache(self):
        self.assertNotIn(self.problem.id, user_tester_ids(self.profile1))

        self.problem.testers.add(self.profile1)

        self.assertIn(self.problem.id, user_tester_ids(self.profile1))

    def test_remove_tester_invalidates_cache(self):
        self.problem.testers.add(self.profile1)
        self.assertIn(self.problem.id, user_tester_ids(self.profile1))

        self.problem.testers.remove(self.profile1)

        self.assertNotIn(self.problem.id, user_tester_ids(self.profile1))

    def test_clear_authors_invalidates_cache(self):
        self.problem.authors.add(self.profile1, self.profile2)
        self.assertIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertIn(self.problem.id, user_editable_ids(self.profile2))

        self.problem.authors.clear()

        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile2))

    def test_set_authors_invalidates_cache_for_old_and_new(self):
        self.problem.authors.add(self.profile1)
        self.assertIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile2))

        self.problem.authors.set([self.profile2])

        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertIn(self.problem.id, user_editable_ids(self.profile2))

    def test_multiple_profiles_independent_cache(self):
        """Adding one profile as author should not affect another's cache."""
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile2))

        self.problem.authors.add(self.profile1)

        self.assertIn(self.problem.id, user_editable_ids(self.profile1))
        self.assertNotIn(self.problem.id, user_editable_ids(self.profile2))
