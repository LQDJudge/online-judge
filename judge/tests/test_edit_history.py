from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from reversion import revisions

from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Problem,
    ProblemData,
    ProblemGroup,
    ProblemSolutionCode,
    ProblemTestCase,
    Profile,
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
)
from judge.views.contests import ContestLog
from judge.views.problem import ProblemLog
from judge.views.quiz import QuizLog


class EditHistoryViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PYHIST",
            defaults={
                "name": "Python History",
                "short_name": "PYHIST",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="history", defaults={"full_name": "History"}
        )

    def setUp(self):
        self.user = User.objects.create_user("history-user", password="password")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def make_problem(self, code="histprob"):
        return Problem.objects.create(
            code=code,
            name="History Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=1.0,
        )

    def test_history_routes_render_for_editors(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        problem = self.make_problem("histrouteprob")
        problem.authors.add(self.profile)
        now = timezone.now()
        contest = Contest.objects.create(
            key="histroutecontest",
            name="History Route Contest",
            start_time=now,
            end_time=now + timezone.timedelta(hours=2),
        )
        contest.authors.add(self.profile)
        quiz = Quiz.objects.create(code="histroutequiz", title="History Route Quiz")
        quiz.authors.add(self.profile)

        urls = [
            reverse("problem_log", args=[problem.code]),
            reverse("contest_log", args=[contest.key]),
            reverse("quiz_log", args=[quiz.code]),
        ]

        for url in urls:
            with self.subTest(url=url, anonymous=True):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

        self.client.force_login(self.user)

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_problem_history_summarizes_testcase_changes(self):
        problem = self.make_problem()
        data = ProblemData.objects.create(problem=problem)

        with revisions.create_revision():
            ProblemTestCase.objects.create(
                dataset=problem,
                order=1,
                type="C",
                input_file="1.in",
                output_file="1.out",
                points=5,
                is_pretest=True,
            )
            data.save()
            revisions.set_user(self.user)
            revisions.set_comment("Updated test data")

        case = problem.cases.get()
        with revisions.create_revision():
            case.points = 10
            case.is_pretest = False
            case.save()
            data.save()
            revisions.set_user(self.user)
            revisions.set_comment("Updated test data")

        view = ProblemLog()
        view.problem = problem
        with override("en"):
            entries = list(view.get_queryset())
        testcase_entries = [
            entry
            for entry in entries
            if getattr(entry, "object_type", None) == "test_case"
        ]

        self.assertTrue(testcase_entries)
        change_text = " ".join(
            change["field"] for entry in testcase_entries for change in entry.changes
        )
        self.assertIn("Changed test case", change_text)
        self.assertIn("Added test case", change_text)
        changed_summary = next(
            change
            for entry in testcase_entries
            for change in entry.changes
            if "Changed test case" in change["field"]
        )
        self.assertIn("\n", changed_summary["old"])
        self.assertIn("\n", changed_summary["new"])
        self.assertFalse(
            any(
                getattr(entry, "object_type", None) == "test_data"
                and not entry.changes
                and entry.revision.comment == "Updated test data"
                for entry in entries
            )
        )

    def test_problem_history_diffs_solution_code_changes(self):
        problem = self.make_problem("histsolutioncode")

        with revisions.create_revision():
            solution_code = ProblemSolutionCode.objects.create(
                problem=problem,
                order=1,
                name="Main",
                source_code="print('old')\n",
                language=self.language,
                expected_result="AC",
            )
            revisions.set_user(self.user)
            revisions.set_comment("Added solution code")

        with revisions.create_revision():
            solution_code.source_code = "print('new')\n"
            solution_code.expected_result = "WA"
            solution_code.save()
            revisions.set_user(self.user)
            revisions.set_comment("Changed solution code")

        view = ProblemLog()
        view.problem = problem
        with override("en"):
            entries = list(view.get_queryset())
        changed_entries = [
            entry
            for entry in entries
            if getattr(entry, "object_type", None) == "solution_code"
            and entry.revision.comment == "Changed solution code"
        ]

        self.assertTrue(changed_entries)
        self.assertTrue(changed_entries[0].changes)
        self.assertTrue(
            any(
                change["field"] == "source code" and change.get("diff_lines")
                for change in changed_entries[0].changes
            )
        )

    def test_contest_history_includes_rows_and_rating_events(self):
        problem = self.make_problem("histcontestprob")
        now = timezone.now()
        contest = Contest.objects.create(
            key="histcontest",
            name="History Contest",
            start_time=now,
            end_time=now + timezone.timedelta(hours=2),
        )
        contest.authors.add(self.profile)

        with revisions.create_revision():
            row = ContestProblem.objects.create(
                contest=contest,
                problem=problem,
                points=100,
                partial=True,
                order=1,
            )
            revisions.add_to_revision(contest)
            revisions.set_user(self.user)
            revisions.set_comment("Edited from site")

        with revisions.create_revision():
            row.points = 200
            row.save()
            revisions.add_to_revision(contest)
            revisions.set_user(self.user)
            revisions.set_comment("Edited from site")

        with revisions.create_revision():
            revisions.add_to_revision(contest)
            revisions.set_user(self.user)
            revisions.set_comment("Rated this contest.")

        view = ContestLog()
        view.object = contest
        with override("en"):
            entries = list(view.get_queryset())
        row_entries = [
            entry
            for entry in entries
            if getattr(entry, "object_type", None) == "contest_row"
        ]

        self.assertTrue(row_entries)
        self.assertTrue(
            any(
                "Changed contest row" in change["field"]
                for entry in row_entries
                for change in entry.changes
            )
        )
        self.assertTrue(
            any(entry.revision.comment == "Rated this contest." for entry in entries)
        )

    def test_quiz_history_summarizes_assignment_changes(self):
        quiz = Quiz.objects.create(code="histquiz", title="History Quiz")
        quiz.authors.add(self.profile)
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="History Question",
            content="2 + 2?",
            choices=[{"id": "a", "text": "4"}],
            correct_answers={"answers": "a"},
        )

        with revisions.create_revision():
            assignment = QuizQuestionAssignment.objects.create(
                quiz=quiz,
                question=question,
                points=1,
                order=0,
            )
            revisions.add_to_revision(quiz)
            revisions.set_user(self.user)
            revisions.set_comment("Added question to quiz")

        with revisions.create_revision():
            assignment.points = 5
            assignment.save()
            revisions.add_to_revision(quiz)
            revisions.set_user(self.user)
            revisions.set_comment("Updated quiz question points")

        view = QuizLog()
        view.object = quiz
        with override("en"):
            entries = list(view.get_queryset())
        assignment_entries = [
            entry
            for entry in entries
            if getattr(entry, "object_type", None) == "quiz_assignment"
        ]

        self.assertTrue(assignment_entries)
        change_text = " ".join(
            change["field"] for entry in assignment_entries for change in entry.changes
        )
        self.assertIn("Added question", change_text)
        self.assertIn("Changed question assignment", change_text)
