from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)


class CodeforcesContestFormatTest(TestCase):
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
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="cf-format", defaults={"full_name": "Codeforces Format"}
        )

    def setUp(self):
        self.user = User.objects.create_user("cf_user", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.start = timezone.now() - timezone.timedelta(hours=1)
        self.contest = Contest.objects.create(
            key="cfformat",
            name="Codeforces Format",
            start_time=self.start,
            end_time=self.start + timezone.timedelta(hours=6),
            format_name="codeforces",
            format_config={"penalty": 50},
            points_precision=2,
            is_visible=True,
        )
        self.participation = ContestParticipation.objects.create(
            contest=self.contest,
            user=self.profile,
            virtual=ContestParticipation.LIVE,
            real_start=self.start,
        )

    def make_problem(self, code, points=500, initial_ac_score=None, order=0):
        problem = Problem.objects.create(
            code=code,
            name=code,
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=points,
            is_public=True,
        )
        return ContestProblem.objects.create(
            contest=self.contest,
            problem=problem,
            points=points,
            initial_ac_score=initial_ac_score,
            order=order,
        )

    def make_submission(self, contest_problem, minute, points, result):
        submission = Submission.objects.create(
            user=self.profile,
            problem=contest_problem.problem,
            language=self.language,
            contest_object=self.contest,
            status="D",
            result=result,
            points=points,
            case_points=points,
            case_total=contest_problem.points,
            time=0.1,
            memory=1024,
        )
        Submission.objects.filter(id=submission.id).update(
            date=self.start + timezone.timedelta(minutes=minute)
        )
        submission.refresh_from_db()
        ContestSubmission.objects.create(
            submission=submission,
            problem=contest_problem,
            participation=self.participation,
            points=points,
        )
        return submission

    def test_accepted_score_interpolates_from_initial_ac_score_to_points(self):
        contest_problem = self.make_problem("cfaccepted", points=500, order=0)
        self.make_submission(contest_problem, minute=10, points=250, result="WA")
        self.make_submission(contest_problem, minute=20, points=500, result="AC")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        problem_data = self.participation.format_data[str(contest_problem.id)]
        self.assertEqual(problem_data["submission_index"], 1)
        self.assertTrue(problem_data["is_ac"])
        self.assertEqual(problem_data["initial_ac_score"], 1667)
        self.assertEqual(problem_data["penalty"], 50)
        self.assertAlmostEqual(problem_data["points"], 1552.1666666666667)
        self.assertEqual(self.participation.score, 1552.17)

    def test_accepted_cell_does_not_display_penalty(self):
        contest_problem = self.make_problem("cfdisplay", points=500, order=0)
        self.make_submission(contest_problem, minute=10, points=250, result="WA")
        self.make_submission(contest_problem, minute=20, points=500, result="AC")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        problem_data = self.participation.format_data[str(contest_problem.id)]
        self.assertEqual(problem_data["penalty"], 50)

        html = str(
            self.contest.format.display_user_problem(
                self.participation, contest_problem
            )
        )
        self.assertIn("1552", html)
        self.assertNotIn("-50", html)
        self.assertNotIn('class="red"', html)

    def test_accepted_score_uses_points_floor(self):
        contest_problem = self.make_problem("cfbase", points=500, order=0)
        self.make_submission(contest_problem, minute=100, points=0, result="WA")
        self.make_submission(contest_problem, minute=200, points=0, result="WA")
        self.make_submission(contest_problem, minute=300, points=500, result="AC")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        problem_data = self.participation.format_data[str(contest_problem.id)]
        self.assertTrue(problem_data["is_ac"])
        self.assertAlmostEqual(problem_data["points"], 594.5)
        self.assertEqual(self.participation.score, 594.5)

    def test_non_accepted_score_uses_best_partial_without_ac_floor(self):
        contest_problem = self.make_problem(
            "cfpartial", points=200, initial_ac_score=500, order=0
        )
        self.make_submission(contest_problem, minute=10, points=80, result="WA")
        self.make_submission(contest_problem, minute=20, points=90, result="WA")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        problem_data = self.participation.format_data[str(contest_problem.id)]
        self.assertFalse(problem_data["is_ac"])
        self.assertEqual(problem_data["points"], 90)
        self.assertEqual(problem_data["partial"], 0.45)
        self.assertEqual(problem_data["time"], 20 * 60)
        self.assertEqual(self.participation.score, 90)

    def test_ce_only_before_freeze_does_not_create_frozen_placeholder(self):
        self.contest.freeze_after = timezone.timedelta(hours=2)
        self.contest.save(update_fields=["freeze_after"])
        contest_problem = self.make_problem("cfcebeforefreeze", points=500, order=0)
        self.make_submission(contest_problem, minute=10, points=0, result="CE")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        self.assertNotIn(str(contest_problem.id), self.participation.format_data)

    def test_ce_only_after_freeze_creates_frozen_placeholder(self):
        self.contest.freeze_after = timezone.timedelta(minutes=5)
        self.contest.save(update_fields=["freeze_after"])
        contest_problem = self.make_problem("cfceafterfreeze", points=500, order=0)
        self.make_submission(contest_problem, minute=10, points=0, result="CE")

        self.participation.recompute_results()
        self.participation.refresh_from_db()

        self.assertEqual(
            self.participation.format_data[str(contest_problem.id)],
            {"time": 0, "points": 0, "frozen": True},
        )

    def test_initial_ac_score_cannot_be_less_than_points(self):
        contest_problem = self.make_problem(
            "cfinvalid", points=100, initial_ac_score=150, order=0
        )
        contest_problem.initial_ac_score = 99

        with self.assertRaises(ValidationError):
            contest_problem.full_clean()
