from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import Contest, Language, Problem, ProblemGroup, Profile
from judge.models.contest import ContestProblem
from judge.models.quiz import Quiz
from judge.review.contest_hashing import compute_contest_input_hash


class ComputeContestInputHashTest(TestCase):
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
        user = User.objects.create_user("ch", "ch@h.com", "x")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )

        cls.p1 = Problem.objects.create(
            code="chp1",
            name="P1",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.p1.authors.add(cls.profile)
        cls.p2 = Problem.objects.create(
            code="chp2",
            name="P2",
            description="y" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=20,
            partial=False,
        )
        cls.p2.authors.add(cls.profile)

        now = timezone.now()
        cls.contest = Contest.objects.create(
            key="chc1",
            name="Hashing Contest",
            description="Test contest.",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        cls.contest.authors.add(cls.profile)
        ContestProblem.objects.create(
            contest=cls.contest, problem=cls.p1, points=100, order=1
        )
        ContestProblem.objects.create(
            contest=cls.contest, problem=cls.p2, points=200, order=2
        )

    def test_stable_for_unchanged_contest(self):
        h1 = compute_contest_input_hash(self.contest)
        h2 = compute_contest_input_hash(self.contest)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_changes_when_contest_description_changes(self):
        h1 = compute_contest_input_hash(self.contest)
        self.contest.description = "Different rules."
        self.contest.save()
        h2 = compute_contest_input_hash(self.contest)
        self.assertNotEqual(h1, h2)

    def test_changes_when_contained_problem_description_changes(self):
        # This is the key cross-cutting property: a change to ANY contained
        # problem's review-relevant fields should dirty the contest hash via
        # compute_input_hash() being mixed into each problem's entry.
        h1 = compute_contest_input_hash(self.contest)
        self.p1.description = "Different statement."
        self.p1.save()
        h2 = compute_contest_input_hash(self.contest)
        self.assertNotEqual(h1, h2)

    def test_changes_when_contest_problem_order_swaps(self):
        h1 = compute_contest_input_hash(self.contest)
        cp1 = ContestProblem.objects.get(contest=self.contest, problem=self.p1)
        cp2 = ContestProblem.objects.get(contest=self.contest, problem=self.p2)
        cp1.order, cp2.order = cp2.order, cp1.order
        cp1.save()
        cp2.save()
        h2 = compute_contest_input_hash(self.contest)
        self.assertNotEqual(h1, h2)

    def test_changes_when_trusted_user_added(self):
        h1 = compute_contest_input_hash(self.contest)
        user2 = User.objects.create_user("ch2", "ch2@h.com", "x")
        profile2, _ = Profile.objects.get_or_create(
            user=user2, defaults={"language": self.language}
        )
        self.contest.testers.add(profile2)
        h2 = compute_contest_input_hash(self.contest)
        self.assertNotEqual(h1, h2)

    def test_ignores_null_problem_slots(self):
        # ContestProblem.problem is nullable (quiz slots have no problem).
        # A null slot must not crash the hash (it used to raise
        # AttributeError on cp.problem.code and 500 the request_public call)
        # and must not affect the hash value, since null slots aren't reviewed.
        h_before = compute_contest_input_hash(self.contest)
        # bulk_create bypasses ContestProblem.save()/full_clean() (which would
        # reject a slot with neither problem nor quiz). Real null-problem slots
        # are quiz slots that reach the DB the same way; the hash only cares
        # that problem is None, so this faithfully reproduces the crash row.
        ContestProblem.objects.bulk_create(
            [ContestProblem(contest=self.contest, problem=None, points=0, order=3)]
        )
        h_after = compute_contest_input_hash(self.contest)
        self.assertEqual(h_before, h_after)

    def test_changes_when_quiz_slot_added(self):
        # A quiz slot IS part of the reviewed surface (the quiz-leak check runs
        # on it), so adding/removing one must dirty the hash — otherwise a
        # non-admin author couldn't re-request review after adding a quiz.
        h_before = compute_contest_input_hash(self.contest)
        quiz = Quiz.objects.create(code="chqz1", title="Hash Quiz")
        ContestProblem.objects.create(
            contest=self.contest, quiz=quiz, points=0, order=3
        )
        h_after = compute_contest_input_hash(self.contest)
        self.assertNotEqual(h_before, h_after)
