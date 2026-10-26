import copy
import json
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, TestCase, override_settings

from judge.models import Problem, ProblemGroup
from judge.review.profile_config import (
    MAX_PROFILE_CONFIG_BYTES,
    ProfileConfigError,
    parse_profile_config,
)
from judge.review.snapshots import (
    build_problem_snapshot,
    problem_review_content,
    preparation_errors,
    read_artifact,
)
from judge.review.types import (
    is_supported_batch_problem,
    problem_profile_labels,
    resolve_problem_profile,
    snapshot_problem_profile,
)


@override_settings(LANGUAGE_CODE="en")
class ProblemJudgingProfileTests(SimpleTestCase):
    def test_standard_including_subtasks_is_preserved(self):
        for settings in (
            {},
            {"checker": "standard"},
            {"checker": "testlibcms"},
            {"checker": "floatsrel"},
        ):
            with self.subTest(settings=settings):
                profile = resolve_problem_profile(settings)
                self.assertEqual(profile.tags, ("standard",))
                self.assertTrue(profile.review_supported)

    def test_known_profiles_are_identified_for_their_preparation_gates(self):
        for settings, tags in (
            ({"use_ioi_signature": True}, ("signature",)),
            ({"checker": "interact"}, ("interactive",)),
            ({"checker": "interacttl"}, ("interactive",)),
            ({"output_only": True}, ("output_only",)),
            ({"output_only": True, "checker": "csv_rmse"}, ("output_only", "metric")),
            ({"checker": "csv_accuracy"}, ("metric",)),
        ):
            with self.subTest(settings=settings):
                profile = resolve_problem_profile(settings)
                self.assertEqual(profile.tags, tags)
                self.assertTrue(profile.review_supported)
                self.assertTrue(is_supported_batch_problem(settings))

    def test_manager_resolves_without_ui_flag(self):
        settings = {
            "communication_manager": "task/manager.cpp",
            "communication_num_processes": 1,
        }
        profile = resolve_problem_profile(settings)
        self.assertEqual(profile.tags, ("communication",))
        self.assertTrue(profile.review_supported)
        self.assertEqual(profile.issues, ())

    def test_frozen_legacy_checker_filename_matches_effective_config(self):
        profile = resolve_problem_profile(
            {"checker": "custom"},
            artifacts={"custom_checker": {"filename": "checker.py"}},
            effective_config={"checker": "checker.py"},
        )
        self.assertEqual(profile.issues, ())

    def test_snapshot_file_manifests_resolve_communication_without_raw_file_fields(
        self,
    ):
        profile = resolve_problem_profile(
            {"is_communication": True, "communication_num_processes": 2},
            artifacts={"communication_manager": {"sha256": "manager"}},
            effective_config={
                "communication": {
                    "manager": {"files": "manager.cpp"},
                    "num_processes": 2,
                }
            },
        )
        self.assertEqual(profile.issues, ())
        self.assertEqual(profile.execution, "communication")

    def test_signature_composes_with_communication_and_interactive(self):
        for settings, expected in (
            (
                {"checker": "interact", "use_ioi_signature": True},
                ("interactive", "signature"),
            ),
            (
                {
                    "communication_manager": "t/m.cpp",
                    "communication_num_processes": 2,
                    "use_ioi_signature": True,
                },
                ("communication", "signature"),
            ),
        ):
            with self.subTest(settings=settings):
                self.assertEqual(resolve_problem_profile(settings).tags, expected)

    def test_binary_or_ai_topic_is_not_a_grading_mode(self):
        profile = resolve_problem_profile(
            {"binary_data": True, "types": ["AI"], "name": "Interactive"}
        )
        self.assertEqual(profile.tags, ("standard",))
        self.assertTrue(profile.binary)

    def test_invalid_process_counts_cannot_fall_back_to_supported_batch(self):
        for count in (None, 0, -1, 17, True, "2", 1.5):
            with self.subTest(count=count):
                profile = resolve_problem_profile(
                    {
                        "communication_manager": "m.cpp",
                        "communication_num_processes": count,
                    }
                )
                self.assertIn("invalid_communication_process_count", profile.issues)
                self.assertFalse(profile.review_supported)

    def test_incomplete_communication_flag_is_not_batch(self):
        self.assertFalse(is_supported_batch_problem({"is_communication": True}))

    def test_effective_mode_cannot_be_hidden_by_authoring_flags(self):
        for effective, tag in (
            ({"interactive": {"files": "i.cpp"}}, "interactive"),
            ({"communication": {"manager": {"files": "m.cpp"}}}, "communication"),
            ({"output_only": False}, "output_only"),
            ({"signature_grader": {"entry": "stub.cpp"}}, "signature"),
            ({"custom_judge": "grader.py"}, "custom"),
        ):
            with self.subTest(effective=effective):
                profile = resolve_problem_profile({}, effective_config=effective)
                self.assertIn(tag, profile.tags)
                self.assertFalse(profile.review_supported)

    def test_effective_mode_mismatch_and_shadowing_are_explicit(self):
        profile = resolve_problem_profile(
            {"output_only": True},
            effective_config={"output_only": True, "interactive": {"files": "i.cpp"}},
        )
        self.assertEqual(profile.execution, "interactive")
        self.assertIn("conflicting_execution_modes", profile.issues)
        self.assertIn("configuration_mismatch_interactive", profile.issues)

    def test_missing_effective_signature_is_not_silently_accepted(self):
        profile = resolve_problem_profile(
            {"use_ioi_signature": True}, effective_config={}
        )
        self.assertIn("configuration_mismatch_signature", profile.issues)

    def test_nested_metric_checker_is_detected(self):
        effective = {
            "test_cases": [{"batched": [{"checker": {"name": "csv_mae", "args": {}}}]}]
        }
        profile = resolve_problem_profile({}, effective_config=effective)
        self.assertTrue(profile.metric)
        self.assertTrue(profile.review_supported)

    def test_unknown_checker_needs_registered_support(self):
        profile = resolve_problem_profile(
            {}, effective_config={"checker": "private.py"}
        )
        self.assertIn("unregistered_checker", profile.issues)

    def test_legacy_checker_filename_matches_saved_artifact(self):
        profile = resolve_problem_profile(
            {"checker": "custom", "custom_checker": "task/check.py"},
            effective_config={"checker": "check.py"},
        )
        self.assertEqual(profile.issues, ())

    def test_effective_process_count_drift_is_reported(self):
        settings = {"communication_manager": "m.cpp", "communication_num_processes": 2}
        profile = resolve_problem_profile(
            settings, effective_config={"communication": {"num_processes": 3}}
        )
        self.assertIn("configuration_mismatch_process_count", profile.issues)

    def test_output_signature_conflict(self):
        profile = resolve_problem_profile(
            {"output_only": True, "use_ioi_signature": True}
        )
        self.assertIn("output_signature_conflict", profile.issues)

    def test_snapshot_round_trip_and_frozen_labels(self):
        profile = resolve_problem_profile(
            {"output_only": True, "checker": "csv_accuracy"}
        )
        snapshot = json.loads(
            json.dumps(
                {
                    "judging_profile": profile.as_dict(),
                    "data_config": {"checker": "standard"},
                }
            )
        )
        self.assertEqual(snapshot_problem_profile(snapshot), profile)
        self.assertEqual(
            list(map(str, problem_profile_labels(snapshot))),
            ["Output-only", "Metric-scored"],
        )

    def test_legacy_snapshot_uses_saved_configuration(self):
        self.assertEqual(
            list(
                map(
                    str,
                    problem_profile_labels(
                        {"data_config": {"use_ioi_signature": True}}
                    ),
                )
            ),
            ["Function / IOI signature"],
        )

    def test_unknown_or_incomplete_saved_profile_cannot_pass(self):
        for profile in (
            {"version": 2},
            {"version": 1},
            [],
            {"version": 1, "issues": None},
        ):
            with self.subTest(profile=profile):
                self.assertFalse(
                    snapshot_problem_profile(
                        {"judging_profile": profile}
                    ).review_supported
                )

    def test_derived_labels_do_not_change_existing_content_identity(self):
        before = {"data_config": {"checker": "standard"}}
        after = copy.deepcopy(before)
        after["judging_profile"] = resolve_problem_profile(
            before["data_config"]
        ).as_dict()
        self.assertEqual(problem_review_content(before), problem_review_content(after))

    def test_preflight_blocks_effective_modes_missing_from_ui(self):
        snapshot = {
            "data_config": {},
            "judging_profile": resolve_problem_profile(
                {}, effective_config={"custom_judge": "x.py"}
            ).as_dict(),
        }
        self.assertTrue(
            any(
                "judging configuration" in message
                for message in preparation_errors(snapshot)
            )
        )


class ProfileConfigSafetyTests(SimpleTestCase):
    def test_reads_normal_configuration_without_executing_it(self):
        self.assertEqual(
            parse_profile_config(
                b"checker: standard\ntest_cases:\n- in: 1.in\n  out: 1.out\n"
            ),
            {"checker": "standard", "test_cases": [{"in": "1.in", "out": "1.out"}]},
        )

    def test_rejects_unsafe_or_ambiguous_yaml(self):
        for raw in (
            b"!!python/object/apply:os.system ['false']",
            b"checker: standard\nchecker: private.py",
            b"a: &x [1]\nb: *x",
            b"[1, 2]",
            b"a: [",
            b"? [1, 2]\n: value",
            b"\xff",
            b"x" * (MAX_PROFILE_CONFIG_BYTES + 1),
            b"a: " + b"[" * 65 + b"0" + b"]" * 65,
            b"a: " + b"9" * 10000,
        ):
            with self.subTest(prefix=raw[:45]):
                with self.assertRaises(ProfileConfigError):
                    parse_profile_config(raw)


@override_settings(LANGUAGE_CODE="en")
class ProblemProfileSnapshotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        group = ProblemGroup.objects.create(name="profile_tests", full_name="Profiles")
        cls.problem = Problem.objects.create(
            code="profilefreeze",
            name="Profile snapshot",
            group=group,
            description="A statement",
            time_limit=1,
            memory_limit=65536,
            points=1,
        )

    def test_effective_configuration_is_bound_to_the_frozen_file(self):
        raw = b"output_only: false\nchecker: standard\n"
        with tempfile.TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            storage.save("profilefreeze/init.yml", ContentFile(raw))
            with patch(
                "judge.review.snapshots.problem_data_storage", storage
            ), override_settings(REVIEW_ARTIFACT_ROOT=directory + "/private"):
                snapshot = build_problem_snapshot(
                    self.problem, include_statement_sources=False
                )
                self.assertEqual(read_artifact(snapshot["artifacts"]["init_yml"]), raw)
                self.assertEqual(problem_profile_labels(snapshot), ["Output-only"])
                self.assertFalse(snapshot_problem_profile(snapshot).review_supported)
                # Editing a live file cannot rewrite the selected run's type.
                storage.delete("profilefreeze/init.yml")
                storage.save(
                    "profilefreeze/init.yml", ContentFile(b"checker: standard\n")
                )
                self.assertEqual(problem_profile_labels(snapshot), ["Output-only"])

    def test_malformed_effective_config_is_preserved_and_blocks_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            storage.save("profilefreeze/init.yml", ContentFile(b"output_only: ["))
            with patch(
                "judge.review.snapshots.problem_data_storage", storage
            ), override_settings(REVIEW_ARTIFACT_ROOT=directory + "/private"):
                snapshot = build_problem_snapshot(
                    self.problem, include_statement_sources=False
                )
                self.assertIn(
                    "invalid_effective_config",
                    snapshot_problem_profile(snapshot).issues,
                )
                self.assertTrue(
                    any(
                        "judging configuration" in error
                        for error in preparation_errors(snapshot)
                    )
                )
