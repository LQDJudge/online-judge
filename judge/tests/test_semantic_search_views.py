from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Language, Problem, ProblemGroup, Profile


@override_settings(USE_ML=True)
class SemanticSearchViewTest(TestCase):
    fixtures = ["language_small"]

    @classmethod
    def setUpTestData(cls):
        language = Language.objects.first()
        cls.user = User.objects.create_user("semantic-user", "semantic@example.com")
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": language}
        )
        cls.problem_group = ProblemGroup.objects.create(
            name="semantic", full_name="Semantic"
        )
        cls.problem = Problem.objects.create(
            code="semanticproblem",
            name="Semantic Problem",
            description="Test problem",
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
            is_public=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_text_search_accepts_post_body(self):
        query = ("dynamic programming " * 300).strip()
        results = [{"code": "abc", "name": "ABC", "score": 0.95}]
        with patch(
            "judge.views.semantic_search.search_problems", return_value=results
        ) as search_problems:
            response = self.client.post(
                reverse("semantic_search_api"),
                {"q": query, "limit": "7"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], results)
        search_problems.assert_called_once_with(query, limit=7)

    def test_text_search_keeps_get_compatibility(self):
        with patch(
            "judge.views.semantic_search.search_problems", return_value=[]
        ) as search_problems:
            response = self.client.get(
                reverse("semantic_search_api"), {"q": "short query", "limit": "3"}
            )

        self.assertEqual(response.status_code, 200)
        search_problems.assert_called_once_with("short query", limit=3)

    def test_similar_problem_search_accepts_post_body(self):
        results = [{"code": "other", "name": "Other", "score": 0.91}]
        with patch(
            "judge.views.semantic_search.similar_problems", return_value=results
        ) as similar_problems:
            response = self.client.post(
                reverse("semantic_search_similar_api"),
                {"problem_id": str(self.problem.id), "limit": "5"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], results)
        similar_problems.assert_called_once_with(self.problem, limit=5)
