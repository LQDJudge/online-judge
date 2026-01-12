from django.core.cache import cache
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone

from judge.models import (
    BlogPost,
    Contest,
    Language,
    Problem,
    ProblemGroup,
    Profile,
)
from judge.models.bookmark import BookMark, get_all_bookmarked_object_ids
from judge.models.problem import Solution


class BookmarkCleanupSignalTestCase(TestCase):
    """Test cases for bookmark cleanup when bookmarkable objects are deleted"""

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
        # Clear cache before each test to ensure isolation
        cache.clear()

        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.user2 = User.objects.create_user(
            username="test_user2", password="password123"
        )
        self.profile2, _ = Profile.objects.get_or_create(
            user=self.user2, defaults={"language": self.language}
        )

    def _create_problem(self, code):
        return Problem.objects.create(
            code=code,
            name=f"Test Problem {code}",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )

    def _create_blogpost(self, slug):
        return BlogPost.objects.create(
            title=f"Test Post {slug}",
            slug=slug,
            content="Test content",
            summary="Test summary",
            publish_on=timezone.now(),
        )

    def _create_contest(self, key):
        from datetime import timedelta

        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=f"Test Contest {key}",
            start_time=now,
            end_time=now + timedelta(hours=2),
        )

    def _create_solution(self, problem):
        return Solution.objects.create(
            problem=problem,
            is_public=True,
            publish_on=timezone.now(),
            content="Test solution content",
        )

    def test_problem_delete_removes_bookmark(self):
        """Test that deleting a problem removes its bookmark"""
        problem = self._create_problem("testprob1")

        # Create bookmark
        bookmark = problem.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)

        # Verify bookmark exists
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 1)

        # Delete problem
        problem.delete()

        # Verify bookmark is deleted
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 0)

    def test_blogpost_delete_removes_bookmark(self):
        """Test that deleting a blogpost removes its bookmark"""
        post = self._create_blogpost("test-post")

        # Create bookmark
        bookmark = post.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)

        # Verify bookmark exists
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 1)

        # Delete blogpost
        post.delete()

        # Verify bookmark is deleted
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 0)

    def test_contest_delete_removes_bookmark(self):
        """Test that deleting a contest removes its bookmark"""
        contest = self._create_contest("testcontest1")

        # Create bookmark
        bookmark = contest.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)

        # Verify bookmark exists
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 1)

        # Delete contest
        contest.delete()

        # Verify bookmark is deleted
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 0)

    def test_solution_delete_removes_bookmark(self):
        """Test that deleting a solution removes its bookmark"""
        problem = self._create_problem("testprob2")
        solution = self._create_solution(problem)

        # Create bookmark
        bookmark = solution.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)

        # Verify bookmark exists
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 1)

        # Delete solution
        solution.delete()

        # Verify bookmark is deleted
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 0)

    def test_delete_removes_bookmark_with_multiple_users(self):
        """Test that deleting an object removes bookmark even when multiple users bookmarked it"""
        problem = self._create_problem("testprob3")

        # Create bookmark with multiple users
        bookmark = problem.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)
        bookmark.add_bookmark(self.profile2)

        # Verify bookmark has both users
        self.assertEqual(bookmark.users.count(), 2)

        # Delete problem
        problem.delete()

        # Verify bookmark is deleted
        self.assertEqual(BookMark.objects.filter(id=bookmark.id).count(), 0)

    def test_delete_without_bookmark_does_not_error(self):
        """Test that deleting an object without a bookmark doesn't cause errors"""
        problem = self._create_problem("testprob4")

        # Don't create any bookmark, just delete
        problem.delete()

        # If we get here without exception, the test passes

    def test_cache_invalidation_on_delete(self):
        """Test that caches are invalidated when a bookmarked object is deleted"""
        problem = self._create_problem("testprob5")

        # Create and bookmark
        bookmark = problem.get_or_create_bookmark()
        bookmark_id = bookmark.id
        bookmark.add_bookmark(self.profile)

        # Populate cache by calling the cached function
        bookmarked_ids = get_all_bookmarked_object_ids(self.profile)
        self.assertIn(bookmark_id, bookmarked_ids)

        # Delete problem - pre_delete signal should invalidate cache before GenericRelation cascade
        problem.delete()

        # Verify the bookmark was deleted from DB (via GenericRelation cascade)
        self.assertFalse(BookMark.objects.filter(id=bookmark_id).exists())

        # Verify the profile no longer has bookmarked objects in DB
        self.assertEqual(self.profile.bookmarked_objects.count(), 0)

        # After deletion, a fresh call should return empty set (cache was dirtied by pre_delete signal)
        fresh_bookmarked_ids = get_all_bookmarked_object_ids(self.profile)
        self.assertNotIn(bookmark_id, fresh_bookmarked_ids)

    def test_unrelated_bookmarks_not_affected(self):
        """Test that deleting one object doesn't affect bookmarks of other objects"""
        problem1 = self._create_problem("testprob6")
        problem2 = self._create_problem("testprob7")

        # Bookmark both problems
        bookmark1 = problem1.get_or_create_bookmark()
        bookmark1.add_bookmark(self.profile)

        bookmark2 = problem2.get_or_create_bookmark()
        bookmark2.add_bookmark(self.profile)

        # Delete first problem
        problem1.delete()

        # Verify first bookmark is deleted, second is not
        self.assertEqual(BookMark.objects.filter(id=bookmark1.id).count(), 0)
        self.assertEqual(BookMark.objects.filter(id=bookmark2.id).count(), 1)
