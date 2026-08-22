from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, override_settings

from judge.utils import submission_results


class FakeStorage:
    location = ""

    def __init__(self):
        self.files = {}

    def url(self, path):
        return "https://cdn.example.test/%s" % path

    def exists(self, path):
        return path in self.files

    def delete(self, path):
        self.files.pop(path, None)

    def save(self, path, content):
        self.files[path] = content.read()
        return path

    def open(self, path, mode="rb"):
        if path not in self.files:
            raise FileNotFoundError(path)
        return BytesIO(self.files[path])


class BucketLikeFakeStorage(FakeStorage):
    bucket = object()


class OverwriteCapableFakeStorage(FakeStorage):
    file_overwrite = False

    def __init__(self):
        super().__init__()
        self.exists_calls = 0
        self.save_file_overwrite_values = []

    def exists(self, path):
        self.exists_calls += 1
        return super().exists(path)

    def save(self, path, content):
        self.save_file_overwrite_values.append(self.file_overwrite)
        if not self.file_overwrite and path in self.files:
            path = "%s_duplicated" % path
        return super().save(path, content)


@override_settings(SECRET_KEY="test-secret")
class SubmissionResultStorageTests(SimpleTestCase):
    def test_result_path_is_opaque(self):
        path = submission_results.submission_result_path(12345)

        self.assertTrue(path.startswith("submission-results/"))
        self.assertTrue(path.endswith("/result.json"))
        self.assertNotIn("12345", path)

    def test_merge_writes_sorted_result_json(self):
        storage = FakeStorage()
        with patch.object(submission_results, "default_storage", storage):
            submission_results.merge_submission_result(
                7,
                [
                    {"case": 2, "output": "two"},
                    {
                        "case": 1,
                        "input": "",
                        "input_available": True,
                        "answer": "ans",
                        "output": "one",
                    },
                ],
            )

            payload = submission_results.load_submission_result(7)

        self.assertEqual(payload["version"], 1)
        self.assertEqual([case["case"] for case in payload["cases"]], [1, 2])
        self.assertTrue(payload["cases"][0]["input_available"])
        self.assertEqual(payload["cases"][0]["answer"], "ans")
        self.assertEqual(payload["cases"][1]["output"], "two")

    def test_file_system_default_storage_round_trip(self):
        with TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root, base_url="/media/")
            with patch.object(submission_results, "default_storage", storage):
                path = submission_results.save_submission_result(
                    8, [{"case": 1, "input": "in", "answer": "ans", "output": "out"}]
                )
                self.assertTrue(submission_results.submission_result_exists(8))
                self.assertTrue(
                    submission_results.submission_result_url(8).startswith("/media/")
                )
                payload = submission_results.load_submission_result(8)

                submission_results.delete_submission_result(8)

        self.assertEqual(path, submission_results.submission_result_path(8))
        self.assertEqual(payload["cases"][0]["input"], "in")
        self.assertEqual(payload["cases"][0]["answer"], "ans")
        self.assertEqual(payload["cases"][0]["output"], "out")

    def test_bucket_like_storage_still_uses_default_storage_api(self):
        storage = BucketLikeFakeStorage()
        with patch.object(submission_results, "default_storage", storage):
            submission_results.save_submission_result(
                12, [{"case": 1, "input": "in", "answer": "ans"}]
            )
            payload = submission_results.load_submission_result(12)

        self.assertEqual(payload["cases"][0]["input"], "in")

    def test_overwrite_capable_storage_saves_exact_path_without_exists(self):
        storage = OverwriteCapableFakeStorage()
        path = submission_results.submission_result_path(13)
        storage.files[path] = b'{"old":true}'
        with patch.object(submission_results, "default_storage", storage):
            saved_path = submission_results.save_submission_result(
                13, [{"case": 1, "output": "out"}]
            )
            payload = submission_results.load_submission_result(13)

        self.assertEqual(saved_path, path)
        self.assertEqual(storage.exists_calls, 0)
        self.assertEqual(storage.save_file_overwrite_values, [True])
        self.assertFalse(storage.file_overwrite)
        self.assertEqual(payload["cases"][0]["output"], "out")

    def test_delete_submission_result_removes_object(self):
        storage = FakeStorage()
        with patch.object(submission_results, "default_storage", storage):
            submission_results.save_submission_result(9, [{"case": 1, "output": "x"}])
            self.assertTrue(submission_results.submission_result_exists(9))

            submission_results.delete_submission_result(9)

            self.assertFalse(submission_results.submission_result_exists(9))

    def test_missing_input_and_answer_are_not_available(self):
        storage = FakeStorage()
        with patch.object(submission_results, "default_storage", storage):
            submission_results.save_submission_result(10, [{"case": 1, "output": "x"}])

            payload = submission_results.load_submission_result(10)

        self.assertFalse(payload["cases"][0]["input_available"])
        self.assertFalse(payload["cases"][0]["answer_available"])

    def test_merge_does_not_overwrite_corrupt_existing_json(self):
        storage = FakeStorage()
        path = submission_results.submission_result_path(11)
        storage.files[path] = b"{"
        with patch.object(submission_results, "default_storage", storage):
            with self.assertRaises(ValueError):
                submission_results.merge_submission_result(
                    11, [{"case": 2, "output": "new"}]
                )

        self.assertEqual(storage.files[path], b"{")
