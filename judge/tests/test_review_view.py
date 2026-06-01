from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_review import ProblemReviewRun


class ReviewDashboardPermissionsTest(TestCase):
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
            name="RG", defaults={"full_name": "Review Group"}
        )
        author_user = User.objects.create_user("auth", "a@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=author_user, defaults={"language": cls.language}
        )
        stranger_user = User.objects.create_user("strn", "s@x.com", "pw")
        cls.stranger, _ = Profile.objects.get_or_create(
            user=stranger_user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rv1",
            name="RV",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.author)
        ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.author,
            input_hash="x" * 64,
        )

    def test_author_can_see_dashboard(self):
        self.client.force_login(self.author.user)
        url = reverse("problem_review_dashboard", args=[self.problem.code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_stranger_gets_403(self):
        self.client.force_login(self.stranger.user)
        url = reverse("problem_review_dashboard", args=[self.problem.code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)
