from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    BlogPost,
    Contest,
    Language,
    Problem,
    ProblemGroup,
    Profile,
)
from judge.models.problem import Solution


class BookmarkAccessibilityTestCase(TestCase):
    """Test cases for bookmark page accessibility filtering"""

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
        cache.clear()

        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.client = Client()
        self.client.login(username="test_user", password="password123")

        self.bookmark_url = reverse("user_bookmark")

    def _create_problem(self, code, is_public=True):
        return Problem.objects.create(
            code=code,
            name=f"Test Problem {code}",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
            is_public=is_public,
        )

    def _create_blogpost(self, slug, visible=True, is_organization_private=False):
        return BlogPost.objects.create(
            title=f"Test Post {slug}",
            slug=slug,
            content="Test content",
            summary="Test summary",
            publish_on=timezone.now() - timedelta(hours=1),
            visible=visible,
            is_organization_private=is_organization_private,
        )

    def _create_contest(self, key, is_visible=True, is_private=False):
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=f"Test Contest {key}",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            is_visible=is_visible,
            is_private=is_private,
        )

    def _create_solution(self, problem, is_public=True, publish_in_past=True):
        publish_on = (
            timezone.now() - timedelta(hours=1)
            if publish_in_past
            else timezone.now() + timedelta(hours=1)
        )
        return Solution.objects.create(
            problem=problem,
            is_public=is_public,
            publish_on=publish_on,
            content="Test solution content",
        )

    def _bookmark_object(self, obj):
        bookmark = obj.get_or_create_bookmark()
        bookmark.add_bookmark(self.profile)
        return bookmark

    def test_accessible_problems_shown(self):
        """Test that accessible (public) problems are shown in bookmarks"""
        problem = self._create_problem("pub1", is_public=True)
        self._bookmark_object(problem)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "problems"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(problem, response.context["bookmarks"])

    def test_inaccessible_problems_filtered(self):
        """Test that inaccessible (private) problems are filtered out"""
        problem = self._create_problem("priv1", is_public=False)
        self._bookmark_object(problem)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "problems"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(problem, response.context["bookmarks"])

    def test_accessible_contests_shown(self):
        """Test that accessible (visible) contests are shown in bookmarks"""
        contest = self._create_contest("vis1", is_visible=True)
        self._bookmark_object(contest)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "contests"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(contest, response.context["bookmarks"])

    def test_inaccessible_contests_filtered(self):
        """Test that inaccessible (private) contests are filtered out"""
        contest = self._create_contest("priv1", is_visible=True, is_private=True)
        self._bookmark_object(contest)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "contests"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(contest, response.context["bookmarks"])

    def test_accessible_blogposts_shown(self):
        """Test that accessible (visible) blog posts are shown in bookmarks"""
        post = self._create_blogpost("vis1", visible=True)
        self._bookmark_object(post)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "posts"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(post, response.context["bookmarks"])

    def test_inaccessible_blogposts_filtered(self):
        """Test that inaccessible (invisible) blog posts are filtered out"""
        post = self._create_blogpost("invis1", visible=False)
        self._bookmark_object(post)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "posts"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(post, response.context["bookmarks"])

    def test_accessible_solutions_shown(self):
        """Test that accessible (public, published) solutions are shown in bookmarks"""
        problem = self._create_problem("sol1", is_public=True)
        solution = self._create_solution(problem, is_public=True, publish_in_past=True)
        self._bookmark_object(solution)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "editorials"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(solution, response.context["bookmarks"])

    def test_private_solutions_filtered(self):
        """Test that private solutions are filtered out"""
        problem = self._create_problem("sol2", is_public=True)
        solution = self._create_solution(problem, is_public=False, publish_in_past=True)
        self._bookmark_object(solution)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "editorials"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(solution, response.context["bookmarks"])

    def test_future_solutions_filtered(self):
        """Test that solutions with future publish date are filtered out"""
        problem = self._create_problem("sol3", is_public=True)
        solution = self._create_solution(problem, is_public=True, publish_in_past=False)
        self._bookmark_object(solution)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "editorials"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(solution, response.context["bookmarks"])

    def test_mixed_accessibility(self):
        """Test that only accessible items are shown when mix of accessible and inaccessible"""
        # Create accessible and inaccessible problems
        public_problem = self._create_problem("pub2", is_public=True)
        private_problem = self._create_problem("priv2", is_public=False)

        self._bookmark_object(public_problem)
        self._bookmark_object(private_problem)

        response = self.client.get(
            self.bookmark_url,
            {"tab": "problems"},
        )

        self.assertEqual(response.status_code, 200)
        bookmarks = response.context["bookmarks"]
        self.assertIn(public_problem, bookmarks)
        self.assertNotIn(private_problem, bookmarks)

    def test_pagination_with_filtering(self):
        """Test that pagination works correctly with accessibility filtering"""
        # Create many public problems
        public_problems = []
        for i in range(25):
            problem = self._create_problem(f"pagepub{i}", is_public=True)
            self._bookmark_object(problem)
            public_problems.append(problem)

        # Create some private problems
        for i in range(5):
            problem = self._create_problem(f"pagepriv{i}", is_public=False)
            self._bookmark_object(problem)

        # First page should have 20 items
        response = self.client.get(
            self.bookmark_url,
            {"tab": "problems", "page": 1},
        )

        self.assertEqual(response.status_code, 200)
        # All items in response should be accessible (public)
        for item in response.context["bookmarks"]:
            self.assertTrue(item.is_public)
