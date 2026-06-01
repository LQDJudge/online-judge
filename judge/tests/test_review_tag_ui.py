from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.models.problem_review import ProblemReviewSubmissionTag


class ReviewTagPostTest(TestCase):
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
            name="TG", defaults={"full_name": "Test Group"}
        )
        u = User.objects.create_user("rt", "r@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=u, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rt1",
            name="RT",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)
        cls.sub = Submission.objects.create(
            user=cls.profile,
            problem=cls.problem,
            language=cls.language,
            status="D",
            result="AC",
            case_points=10,
            case_total=10,
        )

    def test_post_creates_tag(self):
        self.client.force_login(self.profile.user)
        url = reverse("problem_review_tag", args=[self.problem.code])
        resp = self.client.post(
            url,
            {
                "submission_id": self.sub.id,
                "kind": "M",
                "claimed_complexity": "O(N log N)",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            ProblemReviewSubmissionTag.objects.filter(submission=self.sub).exists()
        )

    def test_post_updates_existing_tag(self):
        ProblemReviewSubmissionTag.objects.create(
            submission=self.sub,
            tagged_by=self.profile,
            kind="B",
            claimed_complexity="O(N^2)",
        )
        self.client.force_login(self.profile.user)
        url = reverse("problem_review_tag", args=[self.problem.code])
        resp = self.client.post(
            url,
            {
                "submission_id": self.sub.id,
                "kind": "M",
                "claimed_complexity": "O(N log N)",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            ProblemReviewSubmissionTag.objects.get(submission=self.sub).kind, "M"
        )

    def test_post_creates_tag_without_kind(self):
        self.client.force_login(self.profile.user)
        url = reverse("problem_review_tag", args=[self.problem.code])
        resp = self.client.post(url, {"submission_id": self.sub.id})
        self.assertEqual(resp.status_code, 200)
        tag = ProblemReviewSubmissionTag.objects.get(submission=self.sub)
        self.assertIsNone(tag.kind)

    def test_untag_deletes(self):
        ProblemReviewSubmissionTag.objects.create(
            submission=self.sub, tagged_by=self.profile, kind="M"
        )
        self.client.force_login(self.profile.user)
        url = reverse("problem_review_untag", args=[self.problem.code])
        resp = self.client.post(url, {"submission_id": self.sub.id})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ProblemReviewSubmissionTag.objects.filter(submission=self.sub).exists()
        )
