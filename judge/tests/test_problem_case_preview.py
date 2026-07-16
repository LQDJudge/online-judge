import os
import tempfile
import zipfile
from types import SimpleNamespace

from django.core.cache import cache
from django.test import SimpleTestCase

from judge.utils.problem_data import get_problem_case


class ProblemCasePreviewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _problem_with_zip(self, root, code="preview"):
        os.makedirs(os.path.join(root, code), exist_ok=True)
        return SimpleNamespace(
            code=code,
            data_files=SimpleNamespace(zipfile="%s/testdata.zip" % code),
        )

    def _write_zip(self, root, problem, files):
        path = os.path.join(root, str(problem.data_files.zipfile))
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    def test_binary_file_preview_stops_at_first_invalid_byte(self):
        with tempfile.TemporaryDirectory() as root:
            problem = self._problem_with_zip(root)
            self._write_zip(root, problem, {"answer.npz": b"PK\x03\x04valid\xfftail"})

            with self.settings(
                DMOJ_PROBLEM_DATA_ROOT=root, TESTCASE_VISIBLE_LENGTH=300
            ):
                result = get_problem_case(problem, ["answer.npz"])

        self.assertEqual(result["answer.npz"], "PK\x03\x04valid")

    def test_preview_drops_incomplete_utf8_character_at_read_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            problem = self._problem_with_zip(root)
            self._write_zip(root, problem, {"answer.txt": "aaaé".encode("utf-8")})

            with self.settings(DMOJ_PROBLEM_DATA_ROOT=root, TESTCASE_VISIBLE_LENGTH=1):
                result = get_problem_case(problem, ["answer.txt"])

        self.assertEqual(result["answer.txt"], "a...")
