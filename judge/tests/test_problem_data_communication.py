from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from judge.utils.problem_data import ProblemDataCompiler


class Collection:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class CommunicationProblemDataCompilerTest(SimpleTestCase):
    @patch("judge.utils.problem_data._get_latest_cpp_key", return_value="CPP20")
    def test_emits_cpp_and_java_signature_handlers(self, _latest_cpp_key):
        problem = SimpleNamespace(
            signature_graders=Collection(
                [
                    SimpleNamespace(
                        language="c",
                        handler=SimpleNamespace(name="problem/stub.cpp"),
                        header=SimpleNamespace(name="problem/task.h"),
                    ),
                    SimpleNamespace(
                        language="java",
                        handler=SimpleNamespace(name="problem/grader.java"),
                    ),
                ]
            )
        )
        data = SimpleNamespace(
            generator=None,
            zipfile=None,
            output_limit=None,
            output_prefix=None,
            checker="testlibcms",
            checker_args="{}",
            custom_checker_cpp=None,
            interactive_judge=None,
            fileio_input="",
            fileio_output="",
            output_only=False,
            binary_data=False,
            output_zip_size_mb=None,
            testcase_validator=None,
            communication_manager=SimpleNamespace(name="problem/manager.cpp"),
            communication_num_processes=1,
            use_ioi_signature=True,
        )

        init = ProblemDataCompiler(problem, data, [], []).make_init()

        self.assertEqual(
            init["communication"]["signature"],
            {
                "entry": "stub.cpp",
                "header": "task.h",
                "java": {"entry": "grader.java"},
            },
        )
