from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import Contest, Language, Profile
from judge.models.contest_review import ContestPublicRequest, ContestReviewRun
from judge.review.contest_hashing import compute_contest_input_hash


def _make_language():
    lang, _ = Language.objects.get_or_create(
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
    return lang


class ContestDecisionViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        au = User.objects.create_user("vauthor", "va@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=au, defaults={"language": cls.language}
        )
        adu = User.objects.create_superuser("vadmin", "vad@x.com", "pw")
        cls.admin, _ = Profile.objects.get_or_create(
            user=adu, defaults={"language": cls.language}
        )

    def setUp(self):
        now = timezone.now()
        self.contest = Contest.objects.create(
            key="vdc",
            name="View Decision Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        self.contest.authors.add(self.author)
        ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author,
            input_hash=compute_contest_input_hash(self.contest),
        )
        self.pr = ContestPublicRequest.objects.create(
            contest=self.contest, requested_by=self.author
        )

    def test_non_admin_forbidden(self):
        self.client.force_login(self.author.user)
        resp = self.client.post("/contest/vdc/review/accept", {"feedback": "x"})
        self.assertEqual(resp.status_code, 403)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.PENDING)

    def test_admin_accept(self):
        self.client.force_login(self.admin.user)
        resp = self.client.post("/contest/vdc/review/accept", {"feedback": "great"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.APPROVED)
        self.assertEqual(self.pr.feedback, "great")
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.is_visible)

    def test_admin_reject(self):
        self.client.force_login(self.admin.user)
        resp = self.client.post("/contest/vdc/review/reject", {"feedback": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.REJECTED)

    def test_anonymous_forbidden(self):
        # No login at all — the `not is_authenticated` branch of the guard.
        resp = self.client.post("/contest/vdc/review/accept", {"feedback": "x"})
        self.assertEqual(resp.status_code, 403)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, ContestPublicRequest.PENDING)

    def test_accept_with_no_request_returns_error(self):
        # The pr-is-None branch: admin acts on a contest whose public request
        # was deleted/never existed. Should be 200 with success=False.
        self.pr.delete()
        self.client.force_login(self.admin.user)
        resp = self.client.post("/contest/vdc/review/accept", {"feedback": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["success"])
        self.assertIn("error", resp.json())
