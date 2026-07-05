from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from judge.models import (
    BestSubmission,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)
from judge.performance_points import get_pp_breakdown
from judge.views.user import UserPerformancePointsAjax, UserProblemsPage


class PerformancePointsBreakdownTest(TestCase):
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
        cls.group = ProblemGroup.objects.create(name="pp", full_name="PP")
        cls.user = User.objects.create_user(username="ppuser", password="password")
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )

    def make_problem(self, code, points=100, is_public=True):
        return Problem.objects.create(
            code=code,
            name=code.upper(),
            group=self.group,
            time_limit=1.0,
            memory_limit=262144,
            points=points,
            is_public=is_public,
        )

    def make_submission(self, problem, points, result="AC"):
        return Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status="D",
            result=result,
            case_points=points,
            case_total=100,
            points=points,
        )

    def test_breakdown_uses_best_submission_table(self):
        first_problem = self.make_problem("ppfirst")
        second_problem = self.make_problem("ppsecond")
        private_problem = self.make_problem("ppprivate", points=500, is_public=False)

        first_best = self.make_submission(first_problem, 80)
        self.make_submission(first_problem, 90)
        second_best = self.make_submission(second_problem, 70)
        private_best = self.make_submission(private_problem, 500)

        BestSubmission.objects.create(
            user=self.profile,
            problem=first_problem,
            submission=first_best,
            points=80,
            case_total=100,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=second_problem,
            submission=second_best,
            points=70,
            case_total=100,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=private_problem,
            submission=private_best,
            points=500,
            case_total=100,
        )

        breakdown, has_more = get_pp_breakdown(self.profile, start=0, end=1)

        self.assertTrue(has_more)
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0].problem_code, "ppfirst")
        self.assertEqual(breakdown[0].sub_id, first_best.id)
        self.assertEqual(breakdown[0].points, 80)

        breakdown, has_more = get_pp_breakdown(self.profile, start=1, end=2)

        self.assertFalse(has_more)
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0].problem_code, "ppsecond")

    def test_breakdown_query_is_constant_for_loaded_rows(self):
        problem = self.make_problem("ppquery")
        submission = self.make_submission(problem, 80)
        BestSubmission.objects.create(
            user=self.profile,
            problem=problem,
            submission=submission,
            points=80,
            case_total=100,
        )

        with CaptureQueriesContext(connection) as queries:
            breakdown, has_more = get_pp_breakdown(self.profile, start=0, end=10)

        self.assertEqual(len(queries), 1, [query["sql"] for query in queries])
        self.assertEqual(len(breakdown), 1)
        self.assertFalse(has_more)

    def test_ajax_context_does_not_build_full_solved_page_context(self):
        problem = self.make_problem("ppajax")
        submission = self.make_submission(problem, 80)
        BestSubmission.objects.create(
            user=self.profile,
            problem=problem,
            submission=submission,
            points=80,
            case_total=100,
        )
        request = RequestFactory().get(
            "/user/ppuser/solved/ajax", {"start": "0", "end": "10"}
        )
        request.user = AnonymousUser()
        request.profile = None

        view = UserPerformancePointsAjax()
        view.request = request
        view.object = self.profile
        view.kwargs = {"user": self.user.username}
        view.filter_hidden_result_breakdown = lambda breakdown: breakdown

        with mock.patch.object(
            UserProblemsPage,
            "get_context_data",
            side_effect=AssertionError("full solved-page context should not be built"),
        ):
            context = view.get_context_data(object=self.profile)

        self.assertEqual(len(context["pp_breakdown"]), 1)
