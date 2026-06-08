"""Unit tests for the three v1 contest-review checks.

LLM-based checks are tested with `unittest.mock.patch` to avoid network calls.
Each check is exercised on a single ContestReviewRun fixture so test data is
minimal — the focus is "does the check produce the right status/reason/details
shape" rather than full pipeline behavior (covered separately by the runner
smoke test).
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import (
    Contest,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)
from judge.models.contest import ContestProblem
from judge.models.contest_review import ContestReviewCheckResult, ContestReviewRun
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.contest_checks.problems_reviewed import ProblemsReviewedCheck
from judge.review.contest_checks.submission_leak_check import SubmissionLeakCheck
from judge.review.contest_hashing import compute_contest_input_hash
from judge.review.hashing import compute_input_hash


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


def _make_problem(code, lang, group, author, points=10):
    p = Problem.objects.create(
        code=code,
        name=code.upper(),
        description="x" * 200,
        group=group,
        time_limit=1.0,
        memory_limit=65536,
        points=points,
        partial=False,
    )
    p.authors.add(author)
    return p


def _make_contest(key, author, problems_and_points, is_visible=False):
    now = timezone.now()
    c = Contest.objects.create(
        key=key,
        name=key.upper(),
        description="Test contest",
        start_time=now,
        end_time=now + timedelta(hours=3),
        is_visible=is_visible,
        is_rated=False,
        format_name="default",
    )
    c.authors.add(author)
    for i, (problem, points) in enumerate(problems_and_points, start=1):
        ContestProblem.objects.create(
            contest=c, problem=problem, points=points, order=i
        )
    return c


def _make_run(contest, profile):
    return ContestReviewRun.objects.create(
        contest=contest,
        triggered_by=profile,
        input_hash=compute_contest_input_hash(contest),
    )


def _make_passing_problem_review(problem, author):
    """Create a DONE ProblemReviewRun matching current input_hash, no FAILs."""
    return ProblemReviewRun.objects.create(
        problem=problem,
        triggered_by=author,
        input_hash=compute_input_hash(problem),
        status=ProblemReviewRun.DONE,
        finished_at=timezone.now(),
    )


class ProblemsReviewedCheckTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )
        u = User.objects.create_user("pr", "pr@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=u, defaults={"language": cls.language}
        )
        cls.p1 = _make_problem("prp1", cls.language, cls.group, cls.profile)
        cls.p2 = _make_problem("prp2", cls.language, cls.group, cls.profile)
        cls.contest = _make_contest("prc", cls.profile, [(cls.p1, 100), (cls.p2, 200)])
        cls.review_run = _make_run(cls.contest, cls.profile)

    def test_fail_when_no_problem_review_exists_and_trigger_no_ops(self):
        # Mock the trigger helper so inline-trigger is a no-op — the check
        # then reports "missing" for both problems since nothing materializes.
        # Verifies the per_problem details_json shape + summary counts.
        with patch(
            "judge.review.contest_checks.problems_reviewed.trigger_problem_review_for"
        ) as mock_trigger:
            mock_trigger.return_value = None
            result = ProblemsReviewedCheck().run(self.contest, self.review_run)

        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        self.assertEqual(mock_trigger.call_count, 2)  # one per problem
        per_problem = result.details["per_problem"]
        self.assertEqual(len(per_problem), 2)
        for row in per_problem:
            self.assertEqual(row["verdict"], "missing")
            self.assertTrue(row["triggered_inline"])
        self.assertEqual(result.details["summary"]["missing_after_trigger"], 2)
        self.assertEqual(result.details["summary"]["inline_triggered"], 2)

    def test_default_reuses_fresh_runs(self):
        # Default path (force_refresh_problems=False, i.e. Request Public
        # flow): when both problems have fresh matching runs, the check
        # REUSES them and does not trigger.
        _make_passing_problem_review(self.p1, self.profile)
        _make_passing_problem_review(self.p2, self.profile)
        with patch(
            "judge.review.contest_checks.problems_reviewed.trigger_problem_review_for"
        ) as mock_trigger:
            mock_trigger.return_value = None
            result = ProblemsReviewedCheck().run(self.contest, self.review_run)
        # No triggers fired because both reuses succeeded.
        self.assertEqual(mock_trigger.call_count, 0)
        for row in result.details["per_problem"]:
            self.assertFalse(row["triggered_inline"])
            self.assertEqual(row["verdict"], "pass")

    def test_force_refresh_triggers_every_problem(self):
        # Admin Rerun path (force_refresh_problems=True): re-trigger every
        # contained problem even if a fresh matching run already exists.
        _make_passing_problem_review(self.p1, self.profile)
        _make_passing_problem_review(self.p2, self.profile)
        self.review_run.force_refresh_problems = True
        self.review_run.save(update_fields=["force_refresh_problems"])
        with patch(
            "judge.review.contest_checks.problems_reviewed.trigger_problem_review_for"
        ) as mock_trigger:
            mock_trigger.return_value = None
            result = ProblemsReviewedCheck().run(self.contest, self.review_run)
        self.assertEqual(mock_trigger.call_count, 2)
        for row in result.details["per_problem"]:
            self.assertTrue(row["triggered_inline"])

    def test_fail_when_problem_review_has_failures(self):
        run1 = _make_passing_problem_review(self.p1, self.profile)
        _make_passing_problem_review(self.p2, self.profile)
        ProblemReviewCheckResult.objects.create(
            run=run1,
            check_id="dummy_check",
            status=ProblemReviewCheckResult.FAIL,
            reason="something broke",
        )
        # Default (Request Public) path reuses fresh runs without triggering;
        # the check reads p1's fail verdict from the existing run.
        result = ProblemsReviewedCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        per_problem = result.details["per_problem"]
        verdicts = {row["code"]: row for row in per_problem}
        self.assertEqual(verdicts["prp1"]["verdict"], "fail")
        self.assertEqual(verdicts["prp2"]["verdict"], "pass")
        self.assertIn("dummy_check", verdicts["prp1"]["failing_checks"])
        self.assertEqual(result.details["summary"]["failed"], 1)
        self.assertEqual(result.details["summary"]["passed"], 1)

    def test_stale_run_triggers_but_fresh_run_reuses(self):
        # Default (Request Public) path: a problem with stale hash gets
        # triggered (no reusable match), but a problem with fresh hash is
        # reused without trigger. Verifies the hash-based selective reuse
        # that authors get for free.
        ProblemReviewRun.objects.create(
            problem=self.p1,
            triggered_by=self.profile,
            input_hash="x" * 64,
            status=ProblemReviewRun.DONE,
            finished_at=timezone.now(),
        )
        _make_passing_problem_review(self.p2, self.profile)
        with patch(
            "judge.review.contest_checks.problems_reviewed.trigger_problem_review_for"
        ) as mock_trigger:
            mock_trigger.return_value = None
            result = ProblemsReviewedCheck().run(self.contest, self.review_run)
        # Only p1 (stale) triggered. p2 (fresh) was reused.
        self.assertEqual(mock_trigger.call_count, 1)
        per_problem = {row["code"]: row for row in result.details["per_problem"]}
        self.assertTrue(per_problem["prp1"]["triggered_inline"])
        self.assertFalse(per_problem["prp2"]["triggered_inline"])


class SubmissionLeakCheckTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )
        u = User.objects.create_user("sl", "sl@x.com", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=u, defaults={"language": cls.language}
        )
        cls.p1 = _make_problem("slp1", cls.language, cls.group, cls.author)
        cls.contest = _make_contest("slc", cls.author, [(cls.p1, 100)])
        cls.review_run = _make_run(cls.contest, cls.author)

    def _make_user(self, username, is_superuser=False):
        u = (
            User.objects.create_superuser(username, f"{username}@x.com", "pw")
            if is_superuser
            else User.objects.create_user(username, f"{username}@x.com", "pw")
        )
        p, _ = Profile.objects.get_or_create(
            user=u, defaults={"language": self.language}
        )
        return p

    def _submit(self, profile, problem):
        return Submission.objects.create(
            user=profile,
            problem=problem,
            language=self.language,
            status="D",
            result="AC",
            case_points=10,
            case_total=10,
        )

    def test_pass_when_no_submissions_at_all(self):
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.SUCCESS)

    def test_pass_when_only_author_submitted(self):
        self._submit(self.author, self.p1)
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.SUCCESS)

    def test_pass_when_only_admin_submitted(self):
        admin = self._make_user("adm", is_superuser=True)
        self._submit(admin, self.p1)
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.SUCCESS)

    def test_fail_when_non_trusted_user_submitted(self):
        leaker = self._make_user("randy")
        self._submit(leaker, self.p1)
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        self.assertEqual(result.details["leakers_total"], 1)
        self.assertEqual(result.details["leakers"][0]["username"], "randy")

    def test_fail_when_per_problem_author_outside_contest_team_submitted(self):
        # Regression guard for the trust-scope tightening. A user who is the
        # problem's author but NOT in the contest's authors/curators/testers
        # MUST be flagged — the per-problem trust no longer carries over.
        outsider = self._make_user("oldauthor")
        self.p1.authors.add(outsider)  # author of the problem, but not of THIS contest
        self._submit(outsider, self.p1)
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        usernames = [r["username"] for r in result.details["leakers"]]
        self.assertIn("oldauthor", usernames)

    def test_role_leak_flagged_even_without_submission(self):
        # New signal: a user holding a per-problem role (author/curator/tester)
        # who is NOT in the contest team is flagged even if they've never
        # submitted. Catches the case where a problem's original author has
        # access to source but hasn't (yet) attempted it.
        role_holder = self._make_user("originalauthor")
        self.p1.curators.add(role_holder)
        # No submission from role_holder.
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        self.assertEqual(result.details["leakers_total"], 0)
        self.assertEqual(result.details["role_leakers_total"], 1)
        row = result.details["role_leakers"][0]
        self.assertEqual(row["username"], "originalauthor")
        self.assertEqual(row["problem_code"], "slp1")
        self.assertEqual(row["roles"], ["curator"])

    def test_role_and_submission_signals_both_reported(self):
        # A single contest can have both kinds of leaks at once.
        sub_leaker = self._make_user("subbed")
        role_leaker = self._make_user("haspermission")
        self._submit(sub_leaker, self.p1)
        self.p1.testers.add(role_leaker)
        result = SubmissionLeakCheck().run(self.contest, self.review_run)
        self.assertEqual(result.status, ContestReviewCheckResult.FAIL)
        self.assertEqual(result.details["leakers_total"], 1)
        self.assertEqual(result.details["role_leakers_total"], 1)
