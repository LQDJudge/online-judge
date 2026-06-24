"""Smoke tests for /problems/review/ and /contests/review/.

Covers the things most likely to silently regress:
  - 200 OK for admin and non-admin
  - Scope: non-admin only sees items they author/curate
  - Anonymous gets an empty queryset (the page still renders, by design)
  - Filter combinations don't crash
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import Contest, Language, Problem, ProblemGroup, Profile
from judge.models.contest_review import ContestReviewRun
from judge.models.problem_review import ProblemReviewRun


@override_settings(LANGUAGE_CODE="en")
class ReviewListViewTest(TestCase):
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

        # Three users: admin, author, outsider.
        admin = User.objects.create_superuser("admin1", "a@a.com", "pw")
        author = User.objects.create_user("author1", "b@b.com", "pw")
        outsider = User.objects.create_user("outsider1", "c@c.com", "pw")
        cls.admin_profile, _ = Profile.objects.get_or_create(
            user=admin, defaults={"language": cls.language}
        )
        cls.author_profile, _ = Profile.objects.get_or_create(
            user=author, defaults={"language": cls.language}
        )
        cls.outsider_profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": cls.language}
        )

        # One reviewed problem owned by `author`, one owned by admin.
        cls.p_author = Problem.objects.create(
            code="pa",
            name="Author Problem",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.p_author.authors.add(cls.author_profile)
        ProblemReviewRun.objects.create(
            problem=cls.p_author,
            triggered_by=cls.author_profile,
            input_hash="h1",
        )

        cls.p_admin = Problem.objects.create(
            code="pb",
            name="Admin Problem",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.p_admin.authors.add(cls.admin_profile)
        ProblemReviewRun.objects.create(
            problem=cls.p_admin,
            triggered_by=cls.admin_profile,
            input_hash="h2",
        )

        # One reviewed contest owned by `author`.
        cls.c_author = Contest.objects.create(
            key="ca",
            name="Author Contest",
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=1),
        )
        cls.c_author.authors.add(cls.author_profile)
        ContestReviewRun.objects.create(
            contest=cls.c_author,
            triggered_by=cls.author_profile,
            input_hash="hc1",
        )

    # ------------------------------------------------------------------
    # problem list
    # ------------------------------------------------------------------

    def test_problem_admin_sees_all(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/problems/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {p.id for p in resp.context["items"]}
        self.assertSetEqual(ids, {self.p_author.id, self.p_admin.id})

    def test_problem_author_sees_own_only(self):
        self.client.force_login(self.author_profile.user)
        resp = self.client.get("/problems/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {p.id for p in resp.context["items"]}
        self.assertSetEqual(ids, {self.p_author.id})

    def test_problem_outsider_sees_nothing(self):
        self.client.force_login(self.outsider_profile.user)
        resp = self.client.get("/problems/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["items"])

    def test_problem_search_filter(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/problems/review/", {"search": "Author"})
        ids = {p.id for p in resp.context["items"]}
        self.assertSetEqual(ids, {self.p_author.id})

    def test_problem_verdict_filter_running(self):
        """All test runs default to RUNNING — filter should match both."""
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/problems/review/", {"verdict": "running"})
        ids = {p.id for p in resp.context["items"]}
        self.assertSetEqual(ids, {self.p_author.id, self.p_admin.id})

    def test_problem_verdict_filter_pass_empty(self):
        """No DONE runs in fixture, so verdict=pass returns empty."""
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/problems/review/", {"verdict": "pass"})
        self.assertFalse(resp.context["items"])

    # ------------------------------------------------------------------
    # contest list
    # ------------------------------------------------------------------

    def test_contest_admin_sees_all(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/contests/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_author.id})
        self.assertContains(resp, f'id="contest-row-{self.c_author.id}"')

    def test_contest_author_sees_own(self):
        self.client.force_login(self.author_profile.user)
        resp = self.client.get("/contests/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_author.id})

    def test_contest_list_hides_started_site_public_contests(self):
        now = timezone.now()
        started_public = Contest.objects.create(
            key="startedpublic",
            name="Started Public Contest",
            is_visible=True,
            start_time=now - timezone.timedelta(hours=1),
            end_time=now + timezone.timedelta(hours=1),
        )
        started_public.authors.add(self.author_profile)
        ContestReviewRun.objects.create(
            contest=started_public,
            triggered_by=self.author_profile,
            input_hash="hc-started-public",
        )
        future_public = Contest.objects.create(
            key="futurepublic",
            name="Future Public Contest",
            is_visible=True,
            start_time=now + timezone.timedelta(hours=1),
            end_time=now + timezone.timedelta(hours=2),
        )
        future_public.authors.add(self.author_profile)
        ContestReviewRun.objects.create(
            contest=future_public,
            triggered_by=self.author_profile,
            input_hash="hc-future-public",
        )

        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/contests/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertNotIn(started_public.id, ids)
        self.assertIn(future_public.id, ids)
        self.assertIn(self.c_author.id, ids)

    def test_contest_outsider_sees_nothing(self):
        self.client.force_login(self.outsider_profile.user)
        resp = self.client.get("/contests/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["items"])

    def test_contest_combined_filters(self):
        """Verify combined search + public + verdict don't crash."""
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get(
            "/contests/review/",
            {"search": "Contest", "public": "none", "verdict": "running"},
        )
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_author.id})
