import json
import tempfile

from django.forms import HiddenInput
from django.test import SimpleTestCase
from django.utils.translation import override

from judge.models import ProblemData, ProblemTestCase
from judge.utils.problem_data import ProblemDataCompiler, ProblemDataError
from judge.views.package_import import PackageImportApplyView
from judge.views.problem_data import ProblemCaseForm, ProblemDataForm


class ProblemDataCheckerChoiceTests(SimpleTestCase):
    def test_problem_data_form_hides_python_checker_for_new_data(self):
        form = ProblemDataForm(instance=ProblemData(checker="standard"))

        self.assertNotIn("custom", dict(form.fields["checker"].choices))
        self.assertNotIn("custom_checker", form.fields)

    def test_problem_data_form_preserves_legacy_python_checker(self):
        form = ProblemDataForm(instance=ProblemData(checker="custom"))

        self.assertIn("custom", dict(form.fields["checker"].choices))
        self.assertIsInstance(form.fields["checker"].widget, HiddenInput)
        self.assertNotIn("custom_checker", form.fields)

    def test_problem_case_form_hides_python_checker(self):
        form = ProblemCaseForm(instance=ProblemTestCase(checker="standard"))

        self.assertNotIn("custom", dict(form.fields["checker"].choices))

    def test_package_import_rejects_python_checker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/summary.json", "w") as f:
                json.dump({"checker": {"key": "custom"}}, f)

            with override("en"):
                with self.assertRaisesMessage(
                    ValueError,
                    "Python checkers are no longer supported for imports.",
                ):
                    PackageImportApplyView()._apply_checker(None, tmpdir)

    def test_problem_data_compiler_rejects_python_checker(self):
        data = ProblemData(checker="custom")

        with override("en"):
            with self.assertRaisesMessage(
                ProblemDataError,
                "Python checkers are no longer supported.",
            ):
                ProblemDataCompiler(None, data, [], []).make_init()
