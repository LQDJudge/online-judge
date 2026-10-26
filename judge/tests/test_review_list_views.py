"""Smoke tests for /problems/review/ and /contests/review/.

Covers the things most likely to silently regress:
  - 200 OK for admin and non-admin
  - Scope: non-admin only sees items they author/curate
  - Anonymous is redirected to login
  - Filter combinations don't crash
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from lxml import html

from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Problem,
    ProblemGroup,
    Profile,
)
from judge.models.contest_review import (
    ContestPublicRequest,
    ContestReviewCheckResult,
    ContestReviewRun,
)
from judge.models.problem_review import ProblemReviewRun
from judge.models.public_request import PublicRequest
from judge.review.verdict import batched_verdicts
from judge.views.internal import InternalProblemQueue


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
        ContestPublicRequest.objects.create(
            contest=cls.c_author,
            requested_by=cls.author_profile,
        )

        # Review history can exist without an actionable public request, for
        # example after an author cancels or an admin manually reruns review.
        cls.c_unrequested = Contest.objects.create(
            key="cunrequested",
            name="Unrequested Contest",
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=1),
        )
        cls.c_unrequested.authors.add(cls.author_profile)
        ContestReviewRun.objects.create(
            contest=cls.c_unrequested,
            triggered_by=cls.admin_profile,
            input_hash="hc-unrequested",
        )

    # ------------------------------------------------------------------
    # problem list
    # ------------------------------------------------------------------

    def test_review_lists_require_login(self):
        for path in ("/problems/review/", "/contests/review/"):
            with self.subTest(path=path):
                response = self.client.get(path, {"verdict": "pass"})
                self.assertRedirects(
                    response,
                    "/accounts/login/?next=" + path + "%3Fverdict%3Dpass",
                    fetch_redirect_response=False,
                )

    def test_problem_no_request_includes_contest_membership(self):
        self.client.force_login(self.admin_profile.user)
        ContestProblem.objects.create(
            contest=self.c_author, problem=self.p_author, points=100, order=0
        )
        for visible in (False, True):
            with self.subTest(contest_public=visible):
                Contest.objects.filter(pk=self.c_author.pk).update(is_visible=visible)
                response = self.client.get("/problems/review/", {"public": "none"})
                self.assertSetEqual(
                    {problem.id for problem in response.context["items"]},
                    {self.p_author.id, self.p_admin.id},
                )
                self.assertContains(response, "Not requested", count=3)
                self.assertNotContains(response, "Published via a contest")
                self.assertNotContains(response, 'value="in_contest"')

    def test_problem_public_request_filters_are_independent_of_contest(self):
        self.client.force_login(self.admin_profile.user)
        ContestProblem.objects.create(
            contest=self.c_author, problem=self.p_author, points=100, order=0
        )
        publication_request = PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        for status, value in (
            ("P", "pending"),
            ("A", "approved"),
            ("R", "rejected"),
            ("W", "withdrawn"),
        ):
            with self.subTest(status=status):
                PublicRequest.objects.filter(pk=publication_request.pk).update(
                    status=status
                )
                response = self.client.get("/problems/review/", {"public": value})
                self.assertEqual(
                    [problem.id for problem in response.context["items"]],
                    [self.p_author.id],
                )
                response = self.client.get("/problems/review/", {"public": "none"})
                self.assertEqual(
                    [problem.id for problem in response.context["items"]],
                    [self.p_admin.id],
                )

    def test_already_public_pending_request_is_not_actionable(self):
        publication_request = PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        Problem.objects.filter(pk=self.p_author.pk).update(
            is_public=True, is_organization_private=False
        )
        Problem.dirty_cache(self.p_author.pk)
        for user in (self.admin_profile.user, self.author_profile.user):
            self.client.force_login(user)
            response = self.client.get(
                "/problems/review/", {"search": self.p_author.code}
            )
            self.assertContains(response, "Approved")
            self.assertNotContains(response, "Already public")
            document = html.fromstring(response.content)
            links = document.xpath('//a[contains(@class, "publish-pill")]/@href')
            if user.is_superuser:
                self.assertEqual(len(links), 1)
                queue = self.client.get(links[0])
                self.assertContains(queue, 'id="problem-row-%s"' % self.p_author.pk)
                self.assertEqual(
                    [problem.pk for problem in queue.context["problems"]],
                    [self.p_author.pk],
                )
            else:
                self.assertEqual(links, [])
            response = self.client.get("/problems/review/", {"public": "pending"})
            self.assertNotIn(
                self.p_author.pk, [p.pk for p in response.context["items"]]
            )
        publication_request.refresh_from_db()
        self.assertEqual(publication_request.status, PublicRequest.PENDING)

    def test_public_problem_is_approved_regardless_of_request_history(self):
        Problem.objects.filter(pk=self.p_author.pk).update(
            is_public=True, is_organization_private=False
        )
        Problem.dirty_cache(self.p_author.pk)
        for status in (None, "P", "A", "R", "W"):
            PublicRequest.objects.filter(problem=self.p_author).delete()
            if status is not None:
                PublicRequest.objects.create(
                    problem=self.p_author,
                    requested_by=self.author_profile,
                    status=status,
                )
            for user in (self.admin_profile.user, self.author_profile.user):
                with self.subTest(status=status, user=user.username):
                    self.client.force_login(user)
                    response = self.client.get(
                        "/problems/review/",
                        {"search": self.p_author.code, "public": "approved"},
                    )
                    self.assertEqual(
                        [p.pk for p in response.context["items"]], [self.p_author.pk]
                    )
                    document = html.fromstring(response.content)
                    pills = document.xpath('//*[contains(@class, "publish-pill")]')
                    self.assertEqual(len(pills), 1)
                    self.assertEqual(pills[0].text_content().strip(), "Approved")
                    if status is None or not user.is_superuser:
                        self.assertEqual(pills[0].tag, "span")
                    for value in ("pending", "rejected", "withdrawn", "none"):
                        response = self.client.get(
                            "/problems/review/",
                            {"search": self.p_author.code, "public": value},
                        )
                        self.assertFalse(response.context["items"])
            self.assertEqual(
                PublicRequest.objects.filter(problem=self.p_author)
                .values_list("status", flat=True)
                .first(),
                status,
            )

    def test_organization_private_pending_request_stays_actionable(self):
        self.client.force_login(self.admin_profile.user)
        PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        Problem.objects.filter(pk=self.p_author.pk).update(
            is_public=True, is_organization_private=True
        )
        Problem.dirty_cache(self.p_author.pk)
        response = self.client.get("/problems/review/", {"public": "pending"})
        self.assertEqual([p.pk for p in response.context["items"]], [self.p_author.pk])
        self.assertNotContains(response, "Already public")

    def test_publication_deep_links_ignore_pagination_and_preserve_access(self):
        self.client.force_login(self.admin_profile.user)
        Problem.objects.filter(pk=self.p_admin.pk).update(name="Another pa problem")
        Problem.dirty_cache(self.p_admin.pk)
        target = PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        PublicRequest.objects.create(
            problem=self.p_admin, requested_by=self.admin_profile
        )
        for status in ("P", "A", "R"):
            with self.subTest(status=status):
                PublicRequest.objects.all().update(status=status)
                response = self.client.get(
                    "/problems/review/", {"search": self.p_author.code}
                )
                document = html.fromstring(response.content)
                link = document.xpath(
                    '//a[contains(@class, "publish-pill") and contains(@href, "#problem-row-%s")]/@href'
                    % self.p_author.pk
                )[0]
                with patch.object(InternalProblemQueue, "paginate_by", 1):
                    queue = self.client.get(link)
                self.assertEqual(queue.status_code, 200)
                self.assertEqual(
                    [p.pk for p in queue.context["problems"]], [target.problem_id]
                )
        self.client.force_login(self.author_profile.user)
        self.assertEqual(self.client.get(link).status_code, 403)

    def test_invalid_publication_deep_link_does_not_show_other_requests(self):
        self.client.force_login(self.admin_profile.user)
        PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        for value in ("bad", "-1", "0", "99999999999999999999999999"):
            with self.subTest(value=value):
                response = self.client.get(
                    "/internal/problem_queue",
                    {"tab": "request_public", "problem_id": value},
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["problems"])

    def test_review_list_status_colors(self):
        self.client.force_login(self.admin_profile.user)
        cases = (
            ("READY", False, "D", "verdict-pass"),
            ("NEEDS_AUTHOR_FIX", False, "D", "verdict-fail"),
            ("READY_WITH_WARNINGS", False, "D", "verdict-warning"),
            ("MANUAL_REVIEW", False, "D", "verdict-warning"),
            ("INCOMPLETE", False, "D", "verdict-warning"),
            ("READY", True, "D", "verdict-warning"),
            ("INCOMPLETE", False, "C", "verdict-none"),
            ("INCOMPLETE", False, "R", "verdict-running"),
        )
        for path, run_class, item_field, item in (
            ("/problems/review/", ProblemReviewRun, "problem_id", self.p_author),
            ("/contests/review/", ContestReviewRun, "contest_id", self.c_author),
        ):
            for readiness, stale, status, css_class in cases:
                with self.subTest(
                    path=path, readiness=readiness, stale=stale, status=status
                ):
                    run_class.objects.filter(**{item_field: item.pk}).update(
                        workflow_version=2,
                        readiness=readiness,
                        current_stale=stale,
                        status=status,
                    )
                    response = self.client.get(path, {"search": item.name})
                    self.assertContains(
                        response, 'class="verdict-pill ' + css_class + '"'
                    )
                    if stale:
                        self.assertContains(response, "Needs re-review")
                        self.assertNotContains(response, ">Stale<")

    def test_problem_admin_sees_all(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/problems/review/", {"public": ""})
        self.assertEqual(resp.status_code, 200)
        ids = {p.id for p in resp.context["items"]}
        self.assertSetEqual(ids, {self.p_author.id, self.p_admin.id})

    def test_superuser_defaults_to_pending_on_both_dashboards(self):
        PublicRequest.objects.create(
            problem=self.p_author, requested_by=self.author_profile
        )
        self.client.force_login(self.admin_profile.user)
        for path, expected_id in (
            ("/problems/review/", self.p_author.pk),
            ("/contests/review/", self.c_author.pk),
        ):
            with self.subTest(path=path):
                response = self.client.get(path, follow=True)
                self.assertEqual(
                    response.redirect_chain, [(path + "?public=pending", 302)]
                )
                self.assertEqual(response.context["active_public"], "pending")
                self.assertEqual(
                    [item.pk for item in response.context["items"]], [expected_id]
                )
                response = self.client.get(path, {"public": ""})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["active_public"], "")
                self.assertEqual(len(response.context["items"]), 2)
                response = self.client.get(path, {"search": "Author"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["active_public"], "")

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
    def test_pending_requests_note_earlier_review_with_contest_status_only(self):
        problem_run = ProblemReviewRun.objects.filter(problem=self.p_author).first()
        PublicRequest.objects.create(
            problem=self.p_author,
            requested_by=self.author_profile,
            review_run=problem_run,
        )
        contest_run = ContestReviewRun.objects.filter(contest=self.c_author).first()
        ContestPublicRequest.objects.filter(contest=self.c_author).update(
            review_run=contest_run
        )
        ProblemReviewRun.objects.create(
            problem=self.p_author,
            triggered_by=self.author_profile,
            input_hash="new-problem",
        )
        ContestReviewRun.objects.create(
            contest=self.c_author,
            triggered_by=self.author_profile,
            input_hash="new-contest",
        )
        self.client.force_login(self.admin_profile.user)
        for path in (
            "/problems/review/",
            "/contests/review/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, {"public": "pending"})
                self.assertContains(response, "Request uses an earlier review")
                if path == "/contests/review/":
                    document = html.fromstring(response.content)
                    self.assertEqual(document.xpath("//tbody/tr/td[4]//a"), [])
                else:
                    self.assertContains(response, "#run-status")
                self.assertNotContains(
                    response, "Auto-review shows the saved assessment."
                )

    def test_review_list_columns_link_to_their_own_pages(self):
        for user in (self.admin_profile.user, self.author_profile.user):
            self.client.force_login(user)
            for path, item, detail_url, review_url in (
                (
                    "/problems/review/",
                    self.p_author,
                    "/problem/pa",
                    "/problem/pa/review",
                ),
                (
                    "/contests/review/",
                    self.c_author,
                    "/contest/ca",
                    "/contest/ca/review",
                ),
            ):
                with self.subTest(user=user.username, path=path):
                    response = self.client.get(path, {"search": item.name})
                    document = html.fromstring(response.content)
                    self.assertEqual(
                        document.xpath(
                            '//form[@id="filter-form"]//button[@id="go"]/@type'
                        ),
                        ["submit"],
                    )
                    self.assertEqual(
                        document.xpath(
                            '//select[@id="public"]/option[@value="none"]/text()'
                        ),
                        ["Not requested"],
                    )
                    row = document.xpath(
                        '//table[contains(@class, "review-list-table")]/tbody/tr'
                    )[0]
                    self.assertEqual(row.xpath("./td[1]/a/@href"), [detail_url])
                    self.assertEqual(row.xpath("./td[3]/a/@href"), [review_url])
                    self.assertTrue(
                        row.xpath('./td[3]/a/span[contains(@class, "verdict-pill")]')
                    )
                    self.assertContains(
                        response, '<th class="col-verdict">Auto-review</th>'
                    )

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

    def test_contest_admin_sees_requested_by_default(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/contests/review/", follow=True)
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_author.id})
        self.assertContains(resp, f'id="contest-row-{self.c_author.id}"')

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
    def test_public_contest_is_approved_regardless_of_request_history(self):
        Contest.objects.filter(pk=self.c_author.pk).update(
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
        )
        for status in (None, "P", "A", "R", "W"):
            ContestPublicRequest.objects.filter(contest=self.c_author).delete()
            if status is not None:
                ContestPublicRequest.objects.create(
                    contest=self.c_author,
                    requested_by=self.author_profile,
                    status=status,
                )
            for user in (self.admin_profile.user, self.author_profile.user):
                with self.subTest(status=status, user=user.username):
                    self.client.force_login(user)
                    response = self.client.get(
                        "/contests/review/", {"search": self.c_author.key}
                    )
                    document = html.fromstring(response.content)
                    row = document.get_element_by_id(
                        "contest-row-%s" % self.c_author.pk
                    )
                    self.assertEqual(row.xpath("normalize-space(./td[4])"), "Approved")
                    self.assertEqual(row.xpath("./td[1]/a/@href"), ["/contest/ca"])
                    self.assertEqual(
                        row.xpath("./td[3]/a/@href"), ["/contest/ca/review"]
                    )
                    self.assertEqual(row.xpath("./td[4]//a"), [])
                    approved = self.client.get(
                        "/contests/review/", {"public": "approved"}
                    )
                    self.assertIn(
                        self.c_author.pk, [c.pk for c in approved.context["items"]]
                    )
                    for value in ("pending", "rejected", "withdrawn", "none"):
                        response = self.client.get(
                            "/contests/review/", {"public": value}
                        )
                        self.assertNotIn(
                            self.c_author.pk, [c.pk for c in response.context["items"]]
                        )
            if status is not None:
                self.assertEqual(
                    ContestPublicRequest.objects.get(contest=self.c_author).status,
                    status,
                )

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
    def test_restricted_visible_contest_is_not_displayed_as_publicly_approved(self):
        self.client.force_login(self.admin_profile.user)
        for restriction in ("is_private", "is_organization_private", "is_in_course"):
            Contest.objects.filter(pk=self.c_author.pk).update(
                is_visible=True,
                is_private=False,
                is_organization_private=False,
                is_in_course=False,
            )
            Contest.objects.filter(pk=self.c_author.pk).update(**{restriction: True})
            response = self.client.get("/contests/review/", {"public": "pending"})
            self.assertIn(self.c_author.pk, [c.pk for c in response.context["items"]])
            self.assertNotContains(response, "publish-approved")

    def test_contest_no_request_filter_shows_unrequested_review_history(self):
        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/contests/review/", {"public": "none"})
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_unrequested.id})

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
    def test_contest_review_pagination_retains_search_and_result_filters(self):
        now = timezone.now()
        contests = Contest.objects.bulk_create(
            [
                Contest(
                    key="pagedreview%s" % index,
                    name="Paged review %02d" % index,
                    start_time=now,
                    end_time=now + timezone.timedelta(hours=1),
                )
                for index in range(51)
            ]
        )
        ContestReviewRun.objects.bulk_create(
            [
                ContestReviewRun(
                    contest=contest, workflow_version=2, status="D", readiness="READY"
                )
                for contest in contests
            ]
        )
        self.client.force_login(self.admin_profile.user)
        params = {
            "search": "Paged review",
            "verdict": "pass",
            "public": "none",
            "order": "name",
        }
        first = self.client.get("/contests/review/", params)
        self.assertEqual(len(first.context["items"]), 50)
        document = html.fromstring(first.content)
        links = document.xpath('//a[contains(@href,"page=2")]/@href')
        self.assertTrue(links)
        for link in links:
            self.assertIn("verdict=pass", link)
            self.assertIn("public=none", link)
            self.assertIn("order=name", link)
        second = self.client.get("/contests/review/", {**params, "page": 2})
        self.assertEqual(
            [contest.name for contest in second.context["items"]], ["Paged review 50"]
        )

    def test_contest_review_latest_runs_include_template_fields(self):
        latest_runs, _verdicts = batched_verdicts(
            [self.c_author.id],
            ContestReviewRun,
            ContestReviewCheckResult,
            "contest_id",
        )
        run = latest_runs[self.c_author.id]

        with CaptureQueriesContext(connection) as ctx:
            _ = (run.finished_at, run.started_at, run.status, run.contest_id)

        self.assertEqual(len(ctx), 0)

    def test_contest_author_sees_own(self):
        self.client.force_login(self.author_profile.user)
        resp = self.client.get("/contests/review/")
        self.assertEqual(resp.status_code, 200)
        ids = {c.id for c in resp.context["items"]}
        self.assertSetEqual(ids, {self.c_author.id, self.c_unrequested.id})

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
        ContestPublicRequest.objects.create(
            contest=started_public,
            requested_by=self.author_profile,
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
        ContestPublicRequest.objects.create(
            contest=future_public,
            requested_by=self.author_profile,
        )

        self.client.force_login(self.admin_profile.user)
        resp = self.client.get("/contests/review/", {"public": ""})
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
        self.assertSetEqual(ids, {self.c_unrequested.id})
