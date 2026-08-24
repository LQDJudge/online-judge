from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.translation import override

from judge.forms import ProblemEditForm
from judge.models import Language, Problem, ProblemGroup, ProblemTestCase, Profile
from judge.views.problem_data import ProblemCaseFormSet


@override_settings(DMOJ_PROBLEM_MAX_TOTAL_TIME_LIMIT=2)
class TotalTimeLimitValidationTest(TestCase):
    """The total judging time (time_limit × number of test cases) must not
    exceed DMOJ_PROBLEM_MAX_TOTAL_TIME_LIMIT for non-admins. Enforced both on
    the problem edit form (time limit change) and the problem data formset
    (test case change). Admins are exempt.
    """

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
            name="ttl", defaults={"full_name": "Total Time Limit"}
        )

    def setUp(self):
        self.author = self._make_user("ttl_author")
        self.superuser = self._make_user("ttl_admin", is_superuser=True)

    def _make_user(self, username, *, is_superuser=False):
        user = User.objects.create_user(username=username, password="password123")
        if is_superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save()
        Profile.objects.get_or_create(user=user, defaults={"language": self.language})
        return user

    def _make_problem(self, time_limit=1.0):
        problem = Problem.objects.create(
            code="ttlproblem",
            name="TTL Problem",
            group=self.problem_group,
            time_limit=time_limit,
            memory_limit=65536,
            points=1.0,
        )
        problem.allowed_languages.add(self.language)
        problem.authors.add(self.author.profile)
        return problem

    def _add_cases(self, problem, count, type="C"):
        for i in range(count):
            ProblemTestCase.objects.create(
                dataset=problem, order=i, type=type, points=0, is_pretest=False
            )

    def _problem_post_data(self, problem, **overrides):
        data = {
            "code": problem.code,
            "name": problem.name,
            "is_public": "",
            "organizations": [],
            "date": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
            "authors": [str(p.id) for p in problem.authors.all()],
            "curators": [],
            "testers": [],
            "description": problem.description,
            "types": [],
            "group": str(self.problem_group.id),
            "points": str(problem.points),
            "time_limit": "1.0",
            "memory_limit": "64",
            "memory_unit": "MB",
            "allowed_languages": [str(self.language.id)],
        }
        data.update(overrides)
        return data

    def _formset_data(self, num_cases, **extra):
        data = {
            "cases-TOTAL_FORMS": str(num_cases),
            "cases-INITIAL_FORMS": "0",
            "cases-MIN_NUM_FORMS": "0",
            "cases-MAX_NUM_FORMS": "1000",
        }
        for i in range(num_cases):
            data["cases-%d-order" % i] = str(i)
            data["cases-%d-type" % i] = "C"
            data["cases-%d-batch_scoring" % i] = "sum"
            data["cases-%d-points" % i] = "0"
        data.update(extra)
        return data

    # ---- Problem edit form (time limit save) ----

    def test_problem_save_rejects_when_total_exceeds_for_non_admin(self):
        problem = self._make_problem(time_limit=1.0)
        self._add_cases(problem, 3)  # 1.0 * 3 = 3 > 2
        form = ProblemEditForm(
            data=self._problem_post_data(problem, time_limit="1.0"),
            instance=problem,
            user=self.author,
            profile=self.author.profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("time_limit", form.errors)

    def test_problem_save_allows_when_within_limit_for_non_admin(self):
        problem = self._make_problem(time_limit=1.0)
        self._add_cases(problem, 2)  # 1.0 * 2 = 2 <= 2
        form = ProblemEditForm(
            data=self._problem_post_data(problem, time_limit="1.0"),
            instance=problem,
            user=self.author,
            profile=self.author.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_problem_save_allows_admin_over_limit(self):
        problem = self._make_problem(time_limit=1.0)
        self._add_cases(problem, 10)  # 1.0 * 10 = 10 > 2, but admin is exempt
        form = ProblemEditForm(
            data=self._problem_post_data(problem, time_limit="1.0"),
            instance=problem,
            user=self.superuser,
            profile=self.superuser.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)

    # ---- Problem data formset (test case save) ----

    def test_data_formset_allows_zero_cases_without_zip(self):
        formset = ProblemCaseFormSet(
            data=self._formset_data(0),
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=False,
            require_test_cases=False,
            queryset=ProblemTestCase.objects.none(),
        )
        self.assertTrue(formset.is_valid(), formset.non_form_errors())

    def test_data_formset_rejects_zero_cases_with_zip(self):
        formset = ProblemCaseFormSet(
            data=self._formset_data(0),
            prefix="cases",
            valid_files=["1.in", "1.out"],
            problem_time_limit=1.0,
            enforce_total_time_limit=False,
            require_test_cases=True,
            queryset=ProblemTestCase.objects.none(),
        )
        with override("en"):
            self.assertFalse(formset.is_valid())
            self.assertIn(
                "At least one test case is required.", formset.non_form_errors()
            )

    def test_data_formset_rejects_deleting_only_existing_case(self):
        problem = self._make_problem(time_limit=1.0)
        case = ProblemTestCase.objects.create(
            dataset=problem, order=0, type="C", points=0, is_pretest=False
        )
        data = self._formset_data(1)
        data.update(
            {
                "cases-INITIAL_FORMS": "1",
                "cases-0-id": str(case.id),
                "cases-0-DELETE": "on",
            }
        )
        formset = ProblemCaseFormSet(
            data=data,
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=False,
            require_test_cases=True,
            queryset=ProblemTestCase.objects.filter(id=case.id),
        )
        with override("en"):
            self.assertFalse(formset.is_valid())
            self.assertIn(
                "At least one test case is required.", formset.non_form_errors()
            )

    def test_data_formset_rejects_when_total_exceeds_for_non_admin(self):
        formset = ProblemCaseFormSet(
            data=self._formset_data(3),  # 1.0 * 3 = 3 > 2
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=True,
            queryset=ProblemTestCase.objects.none(),
        )
        self.assertFalse(formset.is_valid())
        self.assertTrue(formset.non_form_errors())

    def test_data_formset_allows_when_within_limit(self):
        formset = ProblemCaseFormSet(
            data=self._formset_data(2),  # 1.0 * 2 = 2 <= 2
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=True,
            queryset=ProblemTestCase.objects.none(),
        )
        self.assertTrue(formset.is_valid(), formset.non_form_errors())

    def test_data_formset_ignores_batch_markers(self):
        # 4 rows but only 2 are real cases (type C); batch start/end don't count.
        data = self._formset_data(4)
        data["cases-0-type"] = "S"
        data["cases-3-type"] = "E"
        formset = ProblemCaseFormSet(
            data=data,  # 2 real cases -> 1.0 * 2 = 2 <= 2
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=True,
            queryset=ProblemTestCase.objects.none(),
        )
        self.assertTrue(formset.is_valid(), formset.non_form_errors())

    def test_data_formset_allows_admin_over_limit(self):
        formset = ProblemCaseFormSet(
            data=self._formset_data(5),  # over limit, but admin is exempt
            prefix="cases",
            valid_files=[],
            problem_time_limit=1.0,
            enforce_total_time_limit=False,
            queryset=ProblemTestCase.objects.none(),
        )
        self.assertTrue(formset.is_valid(), formset.non_form_errors())
