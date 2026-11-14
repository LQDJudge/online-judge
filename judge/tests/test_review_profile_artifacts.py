import copy
import hashlib
import io
import struct
import tempfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase
from django.urls import reverse

from judge.auto_review.artifact_readers import (
    open_output_source,
    safe_zip_members,
    summarize_data,
)

from judge.auto_review.checker_options import checker_configurations
from judge.auto_review.profile_config import mismatched_judging_components
from judge.auto_review.problem_context import _zip_inventory


def source_file(source):
    return {"source": source, "sha256": hashlib.sha256(source.encode()).hexdigest()}


class ProfileArtifactSafetyTests(SimpleTestCase):
    def test_compiled_checker_files_protocol_and_options_must_match_reviewed_sources(
        self,
    ):
        snapshot = {
            "data_config": {"checker": "testlib"},
            "artifacts": {"custom_checker_cpp": {"filename": "checker.cpp"}},
            "effective_profile_config": {
                "checker": {
                    "name": "bridged",
                    "args": {
                        "files": "checker.cpp",
                        "lang": "CPP17",
                        "type": "testlib",
                        "treat_checker_points_as_fraction": True,
                    },
                }
            },
        }
        self.assertEqual(mismatched_judging_components(snapshot), [])
        for key, value in (
            ("files", "other.cpp"),
            ("files", "private/checker.cpp"),
            ("type", "cms"),
            ("treat_checker_points_as_fraction", False),
        ):
            changed = copy.deepcopy(snapshot)
            changed["effective_profile_config"]["checker"]["args"][key] = value
            with self.subTest(key=key, value=value):
                self.assertIn("checker", mismatched_judging_components(changed))

    def test_compiled_manager_and_signature_paths_cannot_point_to_unreviewed_files(
        self,
    ):
        snapshot = {
            "artifacts": {
                "communication_manager": {"filename": "manager.cpp"},
                "signature:python:handler": {"filename": "grader.py"},
            },
            "signature_graders": [{"active": True, "language": "python"}],
            "effective_profile_config": {
                "communication": {
                    "manager": {"files": "manager.cpp"},
                    "signature": {"python": {"entry": "grader.py"}},
                }
            },
        }
        self.assertEqual(mismatched_judging_components(snapshot), [])
        snapshot["effective_profile_config"]["communication"]["manager"][
            "files"
        ] = "other.cpp"
        snapshot["effective_profile_config"]["communication"]["signature"]["python"][
            "entry"
        ] = "other.py"
        self.assertEqual(
            mismatched_judging_components(snapshot), ["manager", "signature"]
        )

    def test_archive_reader_rejects_high_memory_compression_before_opening_members(
        self,
    ):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_LZMA) as archive:
            archive.writestr("one.in", b"1\n")
        raw = data.getvalue()
        with patch(
            "zipfile.ZipFile.open",
            side_effect=AssertionError("Unsafe decompressor started"),
        ):
            with self.assertRaises(ValueError):
                summarize_data(raw, "outputs.zip")
            inventory = _zip_inventory(
                ContentFile(raw, name="tests.zip"),
                [{"id": 1, "type": "C", "input_file": "one.in", "generator_args": ""}],
            )
            self.assertIn("input_preview_unavailable", inventory["records"][0])

    def test_checker_grouping_bounds_context_and_merges_equivalent_options(self):
        configurations = checker_configurations(
            {
                "data_config": {"checker": "standard"},
                "tests": [
                    {
                        "type": "C",
                        "checker": "floatsabs",
                        "checker_args": (
                            '{"precision":6}' if i % 2 else '{ "precision": 6 }'
                        ),
                    }
                    for i in range(2000)
                ],
            }
        )
        self.assertEqual(len(configurations), 1)
        self.assertEqual(configurations[0]["case_count"], 2000)
        self.assertEqual(len(configurations[0]["positions"]), 80)
        self.assertEqual(configurations[0]["omitted_positions"], 1920)

    def test_case_checker_overrides(self):
        configs = checker_configurations(
            {
                "data_config": {"checker": "standard"},
                "tests": [
                    {"type": "C"},
                    {
                        "type": "C",
                        "checker": "csv_accuracy",
                        "checker_args": '{"pretest_fraction":0.5}',
                    },
                ],
            }
        )
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[1]["positions"], [2])

    def test_no_output_url_network_fetch_or_storage_traversal(self):
        for url in (
            "file:///etc/passwd",
            "https://example.org/private.zip",
            "/submission_source_file/../secret",
            "/submission_source_file/%2fetc%2fpasswd",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                open_output_source(url)

    def test_output_reference_reads_registered_storage_only(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            storage.save(
                "submissions/example.zip", ContentFile(b"actual artifact bytes")
            )
            with patch("judge.auto_review.artifact_readers.default_storage", storage):
                with open_output_source(
                    "https://untrusted-host.invalid"
                    + reverse("submission_source_file", args=("example.zip",))
                ) as stream:
                    self.assertEqual(stream.read(), b"actual artifact bytes")

    def test_dataset_readers_do_not_load_objects_or_execute_headers(self):
        def npy(header):
            data = repr(header).encode()
            return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(data)) + data

        observed = summarize_data(
            npy({"shape": (3, 5), "descr": "<f8", "fortran_order": False}), "values.npy"
        )
        self.assertEqual(observed["shape"], [3, 5])
        self.assertFalse(observed["complete_scan"])
        with self.assertRaises(ValueError):
            summarize_data(
                npy({"shape": (1,), "descr": "|O8", "fortran_order": False}),
                "objects.npy",
            )
        observed = summarize_data(b"id,y\na,1\nb,0\n", "train.csv")
        self.assertEqual(observed["sampled_rows"], 3)
        self.assertEqual(observed["column_counts"], [2])

    def test_duplicate_traversing_symlink_and_oversized_archives_rejected(self):
        for names in (("../x",), ("/x",), ("a", "a")):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                for name in names:
                    archive.writestr(name, b"x")
            with zipfile.ZipFile(
                io.BytesIO(data.getvalue())
            ) as archive, self.assertRaises(ValueError):
                safe_zip_members(archive)
        item = zipfile.ZipInfo("huge")
        item.file_size = 256 * 1024 * 1024
        with self.assertRaises(ValueError):
            safe_zip_members(SimpleNamespace(infolist=lambda: [item]))
        item = zipfile.ZipInfo("link")
        item.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            safe_zip_members(SimpleNamespace(infolist=lambda: [item]))
