"""Contest ranking: a hidden-result score cell shows "?" but stays clickable.

build_ranking_profiles renders a masked "?" cell for is_result_hidden problems.
It must still wrap the "?" in the submission popup link (data-featherlight) so a
viewer can open the popup like any other cell -- the popup itself masks score/verdict
details while preserving links the viewer is allowed to open, so nothing leaks.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Quiz,
)
from judge.views.contests import build_ranking_profiles


class ContestRankingHiddenCellTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="Group")
        self.user = self._profile("rkuser")

        now = timezone.now()
        self.contest = Contest.objects.create(
            key="rkhid",
            name="RK",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        self.problem = Problem.objects.create(
            code="rkp1",
            name="RKP1",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        self.cp = ContestProblem.objects.create(
            contest=self.contest,
            problem=self.problem,
            points=100,
            order=1,
            is_result_hidden=True,
        )
        self.part = ContestParticipation.objects.create(
            contest=self.contest,
            user=self.user,
            format_data={str(self.cp.id): {"points": 100, "time": 0}},
        )

    def _profile(self, name):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return p

    def _hidden_cell(self):
        profiles = build_ranking_profiles(self.contest, [self.cp], [self.part])
        return str(profiles[0].problem_cells[0])

    def test_hidden_cell_shows_question_mark_and_is_clickable(self):
        cell = self._hidden_cell()
        self.assertIn("?", cell)  # masked value
        self.assertIn("data-featherlight", cell)  # popup link present -> clickable
        self.assertIn(self.problem.code, cell)  # link targets this problem's popup

    def test_hidden_cell_does_not_leak_score_or_state(self):
        cell = self._hidden_cell()
        self.assertNotIn("100", cell)  # real points hidden
        self.assertNotIn("full-score", cell)  # solved-state class hidden

    def test_hidden_quiz_cell_shows_question_mark_and_is_clickable(self):
        quiz = Quiz.objects.create(code="rkq1", title="RKQ1")
        contest_quiz = ContestProblem.objects.create(
            contest=self.contest,
            quiz=quiz,
            points=100,
            order=2,
            is_result_hidden=True,
        )
        self.part.format_data = {f"quiz_{contest_quiz.id}": {"points": 100, "time": 0}}

        profiles = build_ranking_profiles(self.contest, [contest_quiz], [self.part])
        cell = str(profiles[0].problem_cells[0])

        self.assertIn("?", cell)
        self.assertIn("data-featherlight", cell)
        self.assertIn("quiz-attempts", cell)
        self.assertIn(str(quiz.id), cell)
        self.assertNotRegex(cell, r">\s*100\s*<")
