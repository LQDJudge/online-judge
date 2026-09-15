import json
import tempfile
from unittest.mock import patch

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

    @patch("judge.utils.problem_data._get_latest_cpp_key", return_value="CPP20")
    def test_problem_data_compiler_preserves_file_checker_args(self, _latest_cpp):
        data = ProblemData(
            checker="testlib",
            checker_args=json.dumps(
                {
                    "treat_checker_points_as_percentage": True,
                    "treat_checker_points_as_fraction": False,
                    "time_limit": 10,
                    "memory_limit": 262144,
                    "files": "untrusted.cpp",
                    "lang": "PY3",
                    "type": "cms",
                }
            ),
        )
        data.custom_checker_cpp.name = "problem/checker.cpp"

        checker = ProblemDataCompiler(None, data, [], []).make_init()["checker"]

        self.assertEqual(checker["name"], "bridged")
        self.assertEqual(
            checker["args"],
            {
                "treat_checker_points_as_percentage": True,
                "time_limit": 10,
                "memory_limit": 262144,
                "files": "checker.cpp",
                "lang": "CPP20",
                "type": "testlib",
            },
        )

    @patch("judge.utils.problem_data._get_latest_cpp_key", return_value="CPP20")
    def test_testlib_compiler_uses_default_fraction_mode_without_stored_args(
        self, _latest_cpp
    ):
        for args in ("", "{}"):
            with self.subTest(args=args):
                data = ProblemData(checker="testlib", checker_args=args)
                data.custom_checker_cpp.name = "problem/checker.cpp"

                checker = ProblemDataCompiler(None, data, [], []).make_init()["checker"]

                self.assertNotIn("treat_checker_points_as_fraction", checker["args"])
                self.assertNotIn("treat_checker_points_as_percentage", checker["args"])
                self.assertNotIn("treat_checker_points_as_absolute", checker["args"])
                self.assertEqual(data.checker_args, args)

    @patch("judge.utils.problem_data._get_latest_cpp_key", return_value="CPP20")
    def test_testlib_compiler_preserves_legacy_absolute_mode(self, _latest_cpp):
        data = ProblemData(
            checker="testlib",
            checker_args=json.dumps(
                {
                    "treat_checker_points_as_absolute": True,
                    "treat_checker_points_as_fraction": True,
                }
            ),
        )
        data.custom_checker_cpp.name = "problem/checker.cpp"

        checker = ProblemDataCompiler(None, data, [], []).make_init()["checker"]

        self.assertIs(checker["args"]["treat_checker_points_as_absolute"], True)
        self.assertNotIn("treat_checker_points_as_fraction", checker["args"])

    @patch("judge.utils.problem_data._get_latest_cpp_key", return_value="CPP20")
    def test_fraction_policy_does_not_change_other_cpp_checkers(self, _latest_cpp):
        for kind, adapter in (("testlibcms", "cms"), ("customcpp", "lqdoj")):
            with self.subTest(checker=kind):
                data = ProblemData(checker=kind, checker_args='{"time_limit": 5}')
                data.custom_checker_cpp.name = "problem/checker.cpp"

                checker = ProblemDataCompiler(None, data, [], []).make_init()["checker"]

                self.assertEqual(
                    checker["args"],
                    {
                        "time_limit": 5,
                        "files": "checker.cpp",
                        "lang": "CPP20",
                        "type": adapter,
                    },
                )
