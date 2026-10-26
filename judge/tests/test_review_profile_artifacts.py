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
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from judge.models import Language, Problem, ProblemData, ProblemGroup
from judge.models.problem_data import ProblemSignatureGrader
from judge.review.artifact_readers import (
    open_output_source,
    safe_zip_members,
    summarize_data,
)
from judge.review.coordinator import (
    _metric_score_challenges,
    _custom_checker_challenges,
    _validate,
    output_schema,
    profile_evidence_gaps,
    CoordinatorOutputError,
    _probes,
    _followup_payload,
    _helper_language,
    execute_stage,
    CoordinatorInfrastructureError,
)
from judge.review.checker_options import checker_configurations, metric_public_hints
from judge.review.protocol import required_evidence_protocol
from judge.review.profile_config import mismatched_judging_components
from judge.review.execution import validate_payload
from judge.review.profile_execution import (
    build_profile_package,
    validate_profile_package,
)
from judge.review.provenance import accept_attestation, prepared_binding
from judge.review.snapshots import (
    build_problem_manifest,
    build_problem_snapshot,
    preparation_errors,
    freeze_file,
    SnapshotError,
    _zip_inventory,
)
from judge.review.execution import prepare_extractor_payloads


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

    def test_checker_runtime_uses_frozen_compiler_version_not_latest_available(self):
        snapshot = {
            "effective_profile_config": {
                "checker": {"name": "bridged", "args": {"lang": "CPP17"}}
            },
            "solutions": [
                {
                    "evidence": {
                        "result": "AC",
                        "review_evidence": {
                            "environment": {"runtimes": {"CPP17": [], "CPP23": []}}
                        },
                    }
                }
            ],
        }
        language, assurance = _helper_language(
            snapshot, "checker_probe", {"filename": "checker.py"}
        )
        self.assertEqual(language, "CPP17")
        self.assertTrue(assurance["confirmed"])

    def test_versioned_workers_keep_old_recovery_but_reject_unknown_requests(self):
        run = SimpleNamespace(snapshot={"kind": "problem"})
        for version in range(1, 16):
            with self.subTest(version=version):
                self.assertEqual(
                    execute_stage(
                        run,
                        "EXTRACT_REPAIR",
                        {
                            "kind": "problem",
                            "prompt_version": "cp-coordinator-%s" % version,
                        },
                    ),
                    {"needed": False},
                )
        with self.assertRaises(CoordinatorInfrastructureError):
            execute_stage(
                run, "D", {"kind": "problem", "prompt_version": "cp-coordinator-999"}
            )

    def test_profile_checker_never_silently_falls_back_to_standard(self):
        for checker, args in (
            ("floats", '{"precision":true}'),
            ("unknown", "{}"),
            ("customcpp", "{}"),
        ):
            with self.subTest(checker=checker), self.assertRaises(ValueError):
                build_profile_package(
                    {"data_config": {"checker": checker, "checker_args": args}}, "PY3"
                )
        run = SimpleNamespace(
            snapshot={
                "data_config": {"checker": "interact"},
                "allowed_language_keys": ["PY3"],
            }
        )
        with patch(
            "judge.review.coordinator.build_profile_package",
            side_effect=ValueError("unreproducible"),
        ), patch("judge.review.coordinator._execute_jobs") as execute:
            output = _probes(
                run,
                "P_CHECKER",
                {
                    "proposed_assessment": {
                        "probes": [
                            {
                                "id": "one",
                                "candidate_source": "print(2)",
                                "language": "PY3",
                            }
                        ]
                    }
                },
            )
        self.assertFalse(output["available"])
        execute.assert_not_called()

    def test_binary_checker_metadata_is_kept_for_initial_and_followup_jobs(self):
        run = SimpleNamespace(
            snapshot={"data_config": {"checker": "custom", "binary_data": True}}
        )
        artifact = {
            **source_file("def check(*args, **kwargs): return True"),
            "filename": "checker.py",
        }
        probe = {
            "id": "binary",
            "input": "",
            "expected_output": "",
            "candidate_output": "",
        }
        with patch(
            "judge.review.coordinator._execute_jobs", return_value={}
        ) as execute, patch(
            "judge.review.coordinator._helper_language", return_value=("PY3", {})
        ):
            _probes(
                run,
                "P_CHECKER",
                {
                    "checker": artifact,
                    "data_config": run.snapshot["data_config"],
                    "proposed_assessment": {"probes": [probe]},
                },
            )
        self.assertIs(execute.call_args.args[2][0]["binary_data"], True)
        with patch(
            "judge.review.coordinator._configured_checker_source", return_value=artifact
        ):
            payload = _followup_payload(
                run, {"operation": "checker_probe", "payload": {"cases": [probe]}}
            )
        self.assertIs(payload["binary_data"], True)

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
            with patch("judge.review.execution.read_artifact", return_value=raw):
                prepared = prepare_extractor_payloads(
                    {
                        "artifacts": {"zipfile": {"sha256": "test"}},
                        "tests": [{"id": 1, "type": "C", "input_file": "one.in"}],
                    },
                    {"source": "print('{}')", "language": "PY3"},
                )
            self.assertEqual(prepared["payloads"], [])
            self.assertEqual(len(prepared["unprocessed"]), 1)

    def test_per_artifact_limit_applies_before_snapshot_storage(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            REVIEW_ARTIFACT_ROOT=directory
        ):
            with self.assertRaises(SnapshotError):
                freeze_file(ContentFile(b"x" * 1024, name="outputs.zip"), max_bytes=64)
            self.assertFalse(list(FileSystemStorage(location=directory).listdir("")[1]))

    def test_unreadable_output_formats_need_manual_review_instead_of_a_false_pass(self):
        snapshot = {
            "data_config": {"checker": "standard", "output_only": True},
            "output_examples": [{"archive_summary": {"available": False}}],
        }
        self.assertTrue(profile_evidence_gaps(snapshot, {}))
        snapshot["output_examples"].append({"archive_summary": {"valid": True}})
        self.assertFalse(profile_evidence_gaps(snapshot, {}))

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

    def test_numeric_schema_and_executable_profile_requirements(self):
        _validate(0.5, {"type": "number", "minimum": 0, "maximum": 1})
        for value in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(CoordinatorOutputError):
                _validate(value, {"type": "number", "minimum": 0, "maximum": 1})
        schema = output_schema(
            "H5_PLAN",
            context={
                "judging_profile": {"signature": True},
                "allowed_languages": ["PY3"],
            },
        )
        probe = schema["properties"]["probes"]["items"]
        self.assertIn("candidate_source", probe["required"])
        self.assertIn("language", probe["required"])
        self.assertEqual(probe["properties"]["language"]["enum"], ["PY3"])
        metric = output_schema(
            "H5_PLAN",
            context={
                "judging_profile": {"metric": True},
                "checker_configurations": [
                    {
                        "scope": "checker-1",
                        "checker": "csv_accuracy",
                        "checker_args": '{"pretest_fraction":0.5}',
                    }
                ],
            },
        )
        self.assertIn(
            "expected_pretest_score_min",
            metric["properties"]["probes"]["items"]["required"],
        )

    def test_profile_jobs_require_new_protocol(self):
        self.assertEqual(
            required_evidence_protocol("checker_probe", {"binary_data": True}), 2
        )
        self.assertEqual(required_evidence_protocol("profile_probe", {}), 2)
        self.assertEqual(
            required_evidence_protocol(
                "checker_probe", {"builtin_checker": "csv_accuracy", "checker_args": {}}
            ),
            2,
        )
        self.assertEqual(
            required_evidence_protocol(
                "checker_probe", {"builtin_checker": "standard", "checker_args": {}}
            ),
            1,
        )

    def test_case_checker_overrides_and_public_mask_hints(self):
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
        hint = metric_public_hints(configs)[0]
        self.assertTrue(any(hint["experiment_ids"].values()))
        self.assertFalse(all(hint["experiment_ids"].values()))

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
            with patch("judge.review.artifact_readers.default_storage", storage):
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

    def test_output_attestation_must_verify_actual_bytes(self):
        requested = {
            "package_hash": "p",
            "source_hash": "url",
            "output_artifact_sha256": "actual",
        }
        actual = {
            "artifacts_verified": True,
            "package_hash": "p",
            "source_hash": "url",
            "environment": {"worker": "native"},
        }
        self.assertFalse(accept_attestation(requested, actual)["environment"])
        actual["output_artifact_sha256"] = "actual"
        self.assertTrue(accept_attestation(requested, actual)["environment"])

    def test_profile_packets_cannot_choose_paths_or_host_invocations(self):
        for package in (
            {"execution": "custom", "files": {}},
            {"execution": "batch", "files": {"../private.h": source_file("x")}},
            {"execution": "batch", "files": {}, "shell": "sh"},
            {"execution": "batch", "files": {}, "file_io": {"input": "/etc/passwd"}},
        ):
            with self.subTest(package=package), self.assertRaises(ValueError):
                validate_profile_package(package)

    def test_legal_profile_payload_and_numeric_checker_options(self):
        payload = {
            "source": {**source_file("print(6)"), "language": "PY3"},
            "package": {"execution": "batch", "files": {}},
            "cases": [{"id": "one", "input": "3", "expected_output": "6"}],
        }
        self.assertIs(validate_payload("profile_probe", payload), payload)
        payload["source"] = {**source_file(""), "language": "BUILTIN"}
        payload.update(builtin_checker="floatsabs", checker_args={"precision": 6})
        payload["cases"][0]["candidate_output"] = "6.0000001"
        self.assertIs(validate_payload("checker_probe", payload), payload)
        payload["checker_args"] = {"precision": 10**9}
        with self.assertRaises(ValueError):
            validate_payload("checker_probe", payload)

    def test_score_evidence_requires_distinct_justified_observations(self):
        probes = [
            {"id": str(i), "expected_score_min": score, "expected_score_max": score}
            for i, score in enumerate((0.25, 0.75))
        ]
        cases = [
            {
                "id": str(i),
                "status": "EXIT_ZERO",
                "checker_result": {"passed": False, "points": score},
            }
            for i, score in enumerate((0.25, 0.75))
        ]
        observed = {
            "available": True,
            "probes": probes,
            "jobs": [{"result": {"cases": cases}}],
        }
        self.assertTrue(_metric_score_challenges(observed))
        self.assertFalse(_metric_score_challenges(observed, require_public=True))
        self.assertFalse(_custom_checker_challenges(observed))
        cases[1]["checker_result"]["points"] = 0.1
        self.assertFalse(_metric_score_challenges(observed))


@override_settings(LANGUAGE_CODE="en")
class SignatureSnapshotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        group = ProblemGroup.objects.create(
            name="signature_capture", full_name="Signature capture"
        )
        cls.problem = Problem.objects.create(
            code="signaturecapture",
            name="Signature",
            group=group,
            description="Implement twice",
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        cls.runtime = Language.objects.create(
            key="CPP17",
            name="C++17",
            short_name="C++",
            common_name="C++",
            ace="c_cpp",
            pygments="cpp",
        )
        cls.problem.allowed_languages.add(cls.runtime)
        ProblemData.objects.create(
            problem=cls.problem, checker="standard", use_ioi_signature=True
        )
        ProblemSignatureGrader.objects.create(
            problem=cls.problem,
            language="c",
            handler="signaturecapture/grader.cpp",
            header="signaturecapture/task.h",
        )
        ProblemSignatureGrader.objects.create(
            problem=cls.problem, language="python", handler="signaturecapture/unused.py"
        )

    def test_active_interface_frozen_hashed_and_attested_disabled_family_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            for name, raw in (
                ("grader.cpp", b"int main(){}"),
                ("task.h", b"int twice(int);"),
                ("unused.py", b"private disabled grader"),
                (
                    "init.yml",
                    b"signature_grader:\n  entry: grader.cpp\n  header: task.h\nchecker: standard\n",
                ),
            ):
                storage.save("signaturecapture/" + name, ContentFile(raw))
            with patch(
                "judge.review.snapshots.problem_data_storage", storage
            ), patch.object(
                ProblemSignatureGrader._meta.get_field("handler"), "storage", storage
            ), patch.object(
                ProblemSignatureGrader._meta.get_field("header"), "storage", storage
            ), override_settings(
                REVIEW_ARTIFACT_ROOT=directory + "/private"
            ):
                before = build_problem_snapshot(
                    self.problem, include_statement_sources=False
                )
                self.assertIn("signature:c:handler", before["artifacts"])
                self.assertNotIn("signature:python:handler", before["artifacts"])
                packet = build_profile_package(before, "CPP17")
                self.assertEqual(packet["signature"]["header"], "task.h")
                binding = prepared_binding(self.problem, "source")
                self.assertIn("signature:c:header", binding["requested_artifacts"])
                storage.delete("signaturecapture/unused.py")
                storage.save(
                    "signaturecapture/unused.py", ContentFile(b"disabled edits")
                )
                self.assertEqual(
                    before["content_hash"],
                    build_problem_manifest(self.problem)["content_hash"],
                )
                storage.delete("signaturecapture/init.yml")
                storage.save(
                    "signaturecapture/init.yml",
                    ContentFile(
                        b"signature_grader:\n  entry: grader.cpp\n  header: task.h\n  python:\n    entry: another-disabled.py\nchecker: standard\n"
                    ),
                )
                self.assertEqual(
                    before["content_hash"],
                    build_problem_manifest(self.problem)["content_hash"],
                )
                storage.delete("signaturecapture/task.h")
                storage.save(
                    "signaturecapture/task.h", ContentFile(b"long long twice(int);")
                )
                after = build_problem_manifest(self.problem)
                self.assertNotEqual(before["content_hash"], after["content_hash"])
                self.assertNotEqual(before["package_hash"], after["package_hash"])


class OutputPreparationTests(SimpleTestCase):
    def snapshot(self):
        binding = {
            "package_hash": "p",
            "source_hash": "source",
            "expected_cases": 1,
            "environment": {"native": True},
            "output_artifact_sha256": "artifact",
            "worker_attestation": {"output_artifact_sha256": "artifact"},
        }
        return {
            "kind": "problem",
            "statement": "Predict the values",
            "package_hash": "p",
            "data_config": {"checker": "csv_accuracy", "output_only": True},
            "artifacts": {"zipfile": {"sha256": "tests"}},
            "tests": [{"type": "C"}],
            "output_examples": [
                {
                    "ref": "output:1",
                    "source_hash": "source",
                    "artifact": {"sha256": "artifact"},
                    "evidence": {
                        "status": "D",
                        "result": "WA",
                        "case_points": 0.5,
                        "case_total": 1,
                        "review_evidence": binding,
                        "cases": [
                            {"case": 1, "status": "WA", "points": 0.5, "total": 1}
                        ],
                    },
                }
            ],
        }

    def test_partial_score_upload_needs_no_dummy_source_or_full_ac(self):
        self.assertEqual(preparation_errors(self.snapshot()), [])

    def test_output_portfolio_requires_completed_official_case_coverage(self):
        snapshot = self.snapshot()
        snapshot["output_examples"][0]["evidence"]["cases"] = []
        self.assertTrue(preparation_errors(snapshot))
        snapshot = self.snapshot()
        snapshot["output_examples"][0]["evidence"]["result"] = "RE"
        self.assertTrue(preparation_errors(snapshot))
        snapshot = self.snapshot()
        snapshot["output_examples"][0]["evidence"]["cases"][0]["status"] = "SC"
        self.assertTrue(preparation_errors(snapshot))

    def test_changed_upload_and_missing_worker_hash_block_preparation(self):
        snapshot = self.snapshot()
        snapshot["output_examples"][0]["artifact"]["sha256"] = "changed"
        self.assertTrue(preparation_errors(snapshot))
        snapshot = self.snapshot()
        snapshot["output_examples"][0]["evidence"]["review_evidence"][
            "worker_attestation"
        ] = {}
        self.assertTrue(preparation_errors(snapshot))
