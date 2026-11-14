from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from judge.content_mutation import (
    ContentMutationMiddleware,
    _save_confirmation_token,
    _save_succeeded,
)
from judge.auto_review.execution_freshness import (
    solution_execution_hash,
    solution_result_current,
    validation_execution_hash,
)
from judge.auto_review.preparation import _bound_evidence
from judge.models import (
    Language,
    Problem,
    ProblemData,
    ProblemGroup,
    ProblemSolutionCode,
    Profile,
    Submission,
    SubmissionSource,
)


class ExecutionFingerprintTests(SimpleTestCase):
    def test_each_solution_has_its_own_source_and_language_identity(self):
        problem = SimpleNamespace(pk=1)
        package = {"revision": 2}
        original = solution_execution_hash(problem, "code", 1, package=package)
        self.assertNotEqual(
            original, solution_execution_hash(problem, "changed", 1, package=package)
        )
        self.assertNotEqual(
            original, solution_execution_hash(problem, "code", 2, package=package)
        )
        self.assertEqual(
            original, solution_execution_hash(problem, "code", 1, package=package)
        )

    def test_test_package_revision_invalidates_execution_even_without_new_file_path(
        self,
    ):
        problem = SimpleNamespace(pk=1)
        self.assertNotEqual(
            solution_execution_hash(problem, "code", 1, package={"test_revision": 1}),
            solution_execution_hash(problem, "code", 1, package={"test_revision": 2}),
        )

    @patch("judge.auto_review.execution_freshness._test_configuration", return_value=[])
    @patch("judge.auto_review.execution_freshness._data")
    def test_validation_ignores_checker_revision_but_not_tests_or_validator(
        self, data, tests
    ):
        problem = SimpleNamespace(pk=1)
        config = {"test_revision": 1, "validator_revision": 1, "judging_revision": 1}
        data.return_value = config
        original = validation_execution_hash(problem)
        config["judging_revision"] = 2
        self.assertEqual(original, validation_execution_hash(problem))
        config["validator_revision"] = 2
        self.assertNotEqual(original, validation_execution_hash(problem))
        config["validator_revision"] = 1
        config["test_revision"] = 2
        self.assertNotEqual(original, validation_execution_hash(problem))
        tests.assert_called_with(problem, validation=True)


@override_settings(LANGUAGE_CODE="en")
class SaveConfirmationTests(SimpleTestCase):
    def test_invalid_html_form_is_not_a_successful_save(self):
        self.assertFalse(_save_succeeded(HttpResponse("invalid editor")))
        self.assertFalse(_save_succeeded(JsonResponse({"success": False})))
        self.assertFalse(_save_succeeded(JsonResponse({"status": "error"}, status=400)))
        self.assertTrue(_save_succeeded(JsonResponse({"status": "ok"})))
        self.assertTrue(_save_succeeded(HttpResponseRedirect("/saved")))

    def test_active_review_confirmation_works_without_publication_request(self):
        request = RequestFactory().post(
            "/problem/example/edit", HTTP_ACCEPT="application/json"
        )
        impact = {"problems": [1], "contests": [2], "children": [3]}
        response = ContentMutationMiddleware._confirmation(None, request, impact)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"running review", response.content)
        self.assertIn(b"contest reviews", response.content)
        self.assertIn(b'"requires_review_confirmation": true', response.content)

    def test_confirmation_token_changes_when_a_new_review_started(self):
        old = {"problems": [1], "contests": [], "children": []}
        new = {"problems": [2], "contests": [], "children": []}
        self.assertNotEqual(
            _save_confirmation_token(None, old), _save_confirmation_token(None, new)
        )


@override_settings(LANGUAGE_CODE="en", USE_ML=False)
class ExecutionFreshnessTests(TestCase):
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
            },
        )
        user = User.objects.create_user("freshness-author", password="test")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        group = ProblemGroup.objects.create(name="freshness", full_name="Freshness")
        cls.problem = Problem.objects.create(
            code="freshness1",
            name="Freshness",
            description="Statement",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)
        cls.data = ProblemData.objects.create(problem=cls.problem, checker="standard")

    def reference(
        self, source="correct", stored_source="correct", result="WA", status="D"
    ):
        submission = Submission.objects.create(
            problem=self.problem,
            user=self.profile,
            language=self.language,
            status=status,
            result=result,
        )
        SubmissionSource.objects.create(submission=submission, source=stored_source)
        code = ProblemSolutionCode.objects.create(
            problem=self.problem,
            source_code=source,
            language=self.language,
            expected_result="WA",
            last_submission=submission,
        )
        return code, submission

    def test_old_results_require_a_new_execution(self):
        code, submission = self.reference()
        self.assertFalse(solution_result_current(self.problem, code, submission))
        self.assertFalse(
            _bound_evidence({"execution_hash": "", "current_execution_hash": "current"})
        )
        submission.execution_hash = solution_execution_hash(
            self.problem, code.source_code, code.language_id
        )
        self.assertTrue(solution_result_current(self.problem, code, submission))
        self.assertTrue(
            _bound_evidence(
                {"execution_hash": "current", "current_execution_hash": "current"}
            )
        )
        self.assertFalse(
            _bound_evidence(
                {"execution_hash": "old", "current_execution_hash": "current"}
            )
        )

    def test_narrow_validator_revision_leaves_solution_results_current(self):
        before_solution = solution_execution_hash(
            self.problem, "code", self.language.pk
        )
        before_validation = validation_execution_hash(self.problem)
        self.data.testcase_validator_language = "python"
        self.data.save()
        self.assertEqual(
            before_solution,
            solution_execution_hash(self.problem, "code", self.language.pk),
        )
        self.assertNotEqual(before_validation, validation_execution_hash(self.problem))
