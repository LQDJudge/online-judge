import copy
import hashlib
import io
import json
import os
import tempfile
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings

from ai_features.markdown_improver import MarkdownImprover
from llm_service.llm_api import LLMService
from llm_service.prompt_guidelines import (
    MARKDOWN_FORMATTING_RULES,
    PROBLEM_STATEMENT_FORMAT_RULES,
    get_markdown_rules_for_prompt,
)

from judge.review.coordinator import (
    CONTEST_WRITING,
    PROMPT_VERSION,
    SYSTEM,
    CoordinatorInfrastructureError,
    CoordinatorOutputError,
    ReviewExecutionPending,
    _bounded_cases,
    _custom_checker_challenges,
    _dossier,
    _deduplicate_contest_synthesis,
    _call,
    _response_correction,
    _extract_failed,
    _extract,
    _source,
    _helper_language,
    _probes,
    _configured_checker_source,
    _followup_payload,
    _summarize_extraction,
    build_stage_input,
    effective_readiness,
    execute_stage,
    stage_definitions,
    stage_dependencies_ready,
    validate_output,
)
from judge.review.snapshots import (
    SnapshotError,
    _zip_inventory,
    freeze_file,
    preparation_errors,
    read_artifact,
    canonical_child_content,
    _manifest_artifact,
    attachment_content_identity,
    case_input_manifest,
    digest_json,
    execution_package_identity,
    problem_review_content,
    problem_review_input_hash,
    reviewed_content_hash,
    _bound_evidence,
)


def run_fixture(outputs=None, snapshot=None):
    stages = MagicMock()
    stages.all.return_value = [
        SimpleNamespace(stage_id=name, status="D", output_json=value)
        for name, value in (outputs or {}).items()
    ]
    stages.filter.return_value.first.return_value = None
    return SimpleNamespace(
        pk=1,
        snapshot=snapshot or {"kind": "problem"},
        coordinator_stages=stages,
        triggered_by_id=None,
    )


def section(status="no_issue", findings=None):
    return {
        "status": status,
        "summary": "Reviewed",
        "findings": findings or [],
        "uncertainties": [],
    }


async def empty_poe_stream():
    if False:
        yield None


class CoordinatorBoundaryTests(SimpleTestCase):
    def test_contest_synthesis_sends_each_evidence_dossier_only_once(self):
        evidence = {
            "children": [
                {
                    "analysis": {
                        "H7": {"comparisons": [{"candidate_ref": "candidate:known"}]}
                    }
                }
            ],
            "statement": "large statement " * 10000,
        }
        outputs = {
            "D": {"complete": True},
            "E": evidence,
            "H1": section(),
            "H2": section(),
        }
        run = run_fixture(outputs, {"kind": "contest"})
        q_input = build_stage_input(run, "Q")
        dossier = execute_stage(run, "Q", q_input)
        outputs["Q"] = dossier
        context = build_stage_input(run_fixture(outputs, {"kind": "contest"}), "R")
        self.assertNotIn("stages", context)
        self.assertEqual(context["dossier"], dossier)
        self.assertLess(len(json.dumps(context)), 200000)
        # Nested child refs remain valid, including for a resumed old envelope.
        finding = {
            "status": "finding",
            "severity": "warning",
            "confidence": "high",
            "claim": "Duplicate",
            "reasoning": "Supplied comparison",
            "evidence_refs": ["stage:H7", "candidate:known"],
            "execution_confirmation": False,
            "suggested_probe_or_fix": "Review",
            "needs_human": True,
        }
        response = {
            "readiness": "MANUAL_REVIEW",
            "summary": "Review",
            "author_feedback": "Review",
            "findings": [finding],
            "uncertainties": [],
            "actions": [],
            "follow_up_requests": [],
        }
        validate_output(response, "R", context)
        legacy = {**context, "stages": outputs}
        frozen = copy.deepcopy(legacy)
        compact = _deduplicate_contest_synthesis(legacy)
        self.assertEqual(compact["stages"], {})
        self.assertEqual(legacy, frozen)
        legacy["stages"]["extra"] = {"summary": "Unique evidence"}
        compact = _deduplicate_contest_synthesis(legacy)
        self.assertEqual(compact["stages"], {"extra": {"summary": "Unique evidence"}})
        self.assertEqual(
            _deduplicate_contest_synthesis({"kind": "problem"}), {"kind": "problem"}
        )

    @patch("judge.review.coordinator._execute_jobs")
    def test_builtin_checker_probes_use_native_implementation(self, jobs):
        jobs.return_value = {"available": True, "jobs": [{"state": "done"}]}
        proposals = [
            {
                "id": "valid",
                "input": "1",
                "expected_output": "2",
                "candidate_output": "2",
            }
        ]
        result = _probes(
            run_fixture(),
            "P_CHECKER",
            {
                "data_config": {"checker": "standard"},
                "proposed_assessment": {"probes": proposals},
            },
        )
        self.assertTrue(result["available"])
        payload = jobs.call_args.args[2][0]
        self.assertEqual(payload["builtin_checker"], "standard")
        self.assertEqual(payload["source"]["language"], "BUILTIN")
        self.assertEqual(payload["cases"], proposals)
        jobs.reset_mock()
        for config in (
            {"checker": "custom"},
            {"checker": "standard", "checker_args": '{"_checker":1}'},
        ):
            result = _probes(
                run_fixture(),
                "P_CHECKER",
                {
                    "data_config": config,
                    "proposed_assessment": {"probes": proposals},
                },
            )
            self.assertFalse(result["available"])
        jobs.assert_not_called()

    def test_builtin_followup_binds_frozen_checker_not_model_source(self):
        payload = _followup_payload(
            run_fixture(snapshot={"data_config": {"checker": "standard"}}),
            {
                "operation": "checker_probe",
                "payload": {
                    "cases": [
                        {
                            "id": "probe",
                            "input": "1",
                            "expected_output": "2",
                            "candidate_output": "2",
                        }
                    ],
                    "source": {"source": "raise Exception()"},
                    "builtin_checker": "bridged",
                    "checker_args": {"_checker": "injected"},
                },
            },
        )
        self.assertEqual(payload["builtin_checker"], "standard")
        self.assertEqual(payload["checker_args"], {})
        self.assertEqual(payload["source"]["source"], "")

    def test_preflight_handles_absent_optional_artifacts(self):
        snapshot = {
            "kind": "problem",
            "statement": "Sum the array.",
            "artifacts": {
                "generator": None,
                "custom_checker": None,
                "custom_checker_cpp": None,
                "testcase_validator": {"size": None},
            },
            "data_config": {"checker": "standard"},
        }
        context = {"prompt_version": PROMPT_VERSION, "snapshot": snapshot}
        with patch(
            "judge.review.coordinator.preparation_errors", return_value=[]
        ), patch.object(LLMService, "call_llm") as call:
            output = execute_stage(run_fixture(snapshot=snapshot), "D", context)
        self.assertTrue(output["complete"])
        call.assert_not_called()

    def test_missing_required_artifacts_remain_preflight_errors_not_infrastructure(
        self,
    ):
        snapshot = {
            "kind": "problem",
            "statement": "Sum the array.",
            "artifacts": {
                "generator": None,
                "testcase_validator": None,
                "custom_checker": None,
                "custom_checker_cpp": None,
            },
            "data_config": {"checker": "standard"},
        }
        context = {"prompt_version": PROMPT_VERSION, "snapshot": snapshot}
        output = execute_stage(run_fixture(snapshot=snapshot), "D", context)
        self.assertFalse(output["complete"])
        self.assertTrue(output["errors"])

    @patch("judge.review.coordinator._source")
    def test_checker_source_follows_config_not_leftover_files(self, source):
        snapshot = {
            "data_config": {"checker": "custom", "checker_args": '{"epsilon":0.1}'}
        }
        source.return_value = {
            "source": "def check(*args, **kwargs): return True",
            "sha256": "frozen",
            "filename": "checker.py",
        }
        _configured_checker_source(snapshot)
        source.assert_called_once_with(snapshot, "custom_checker")
        source.reset_mock()
        payload = _followup_payload(
            run_fixture(snapshot=snapshot),
            {
                "operation": "checker_probe",
                "payload": {
                    "source": "ignored model code",
                    "language": "PY3",
                    "cases": [],
                },
            },
        )
        self.assertEqual(payload["checker_name"], "custom")
        self.assertEqual(payload["checker_args"], '{"epsilon":0.1}')
        source.assert_called_once_with(snapshot, "custom_checker")
        source.reset_mock()
        snapshot["data_config"]["checker"] = "standard"
        self.assertIsNone(_configured_checker_source(snapshot))
        source.assert_not_called()

    def test_contest_can_cite_actual_supplied_child_analysis_not_invented_refs(self):
        output = section(
            "finding",
            [
                {
                    "status": "finding",
                    "severity": "warning",
                    "confidence": "high",
                    "claim": "Duplicate",
                    "reasoning": "Supplied child comparison",
                    "evidence_refs": ["stage:H7", "candidate:known"],
                    "execution_confirmation": False,
                    "suggested_probe_or_fix": "Manual decision",
                    "needs_human": True,
                }
            ],
        )
        context = {
            "kind": "contest",
            "available_refs": [],
            "dossier": {
                "children": [
                    {
                        "analysis": {
                            "H7": {
                                "comparisons": [{"candidate_ref": "candidate:known"}]
                            }
                        }
                    }
                ]
            },
        }
        validate_output(output, "H2", context)
        output["findings"][0]["evidence_refs"] = ["candidate:invented"]
        with self.assertRaises(CoordinatorOutputError):
            validate_output(output, "H2", context)

    def test_semantic_retrieval_is_independent_of_blind_analysis(self):
        self.assertNotIn("S", stage_definitions("problem")["E"]["dependencies"])
        self.assertIn("S", stage_definitions("problem")["H7"]["required_outputs"])

    def test_soft_dependency_errors_are_terminal_but_preflight_errors_block(self):
        stages = {"D": SimpleNamespace(status="D"), "FG": SimpleNamespace(status="E")}
        self.assertTrue(
            stage_dependencies_ready(stage_definitions("problem")["H1"], stages)
        )
        stages["FG"].status = "P"
        self.assertFalse(
            stage_dependencies_ready(stage_definitions("problem")["H1"], stages)
        )
        stages["FG"].status = "E"
        stages["D"].status = "E"
        self.assertFalse(
            stage_dependencies_ready(stage_definitions("problem")["H1"], stages)
        )

    def test_failed_output_is_omitted_and_missing_evidence_propagates(self):
        run = run_fixture({"D": {"complete": True}})
        run.coordinator_stages.all.return_value.extend(
            [
                SimpleNamespace(
                    stage_id="FG",
                    status="E",
                    output_json={"summary": "stale source"},
                    error_category="provider",
                ),
                SimpleNamespace(
                    stage_id="H2",
                    status="D",
                    output_json={
                        "summary": "Partial",
                        "_missing_evidence": [
                            {
                                "stage_id": "E",
                                "reason": "infrastructure_failure",
                                "category": "provider",
                            }
                        ],
                    },
                ),
            ]
        )
        context = build_stage_input(run, "H3")
        self.assertEqual(context["independent_coordinator"], {})
        self.assertEqual(context["solution_analysis"]["summary"], "Partial")
        self.assertEqual(
            {item["stage_id"] for item in context["missing_evidence"]}, {"FG", "E"}
        )
        self.assertNotIn("stage:FG", context["available_refs"])
        self.assertNotIn("stale source", json.dumps(context))

    @patch("judge.review.coordinator._call")
    @patch("judge.review.coordinator._probes")
    def test_required_missing_inputs_do_not_call_model_or_execute_probes(
        self, probes, call
    ):
        for stage_id in ("P_VALIDATOR", "P_CHECKER", "H7"):
            with self.subTest(stage_id=stage_id):
                context = {
                    "prompt_version": PROMPT_VERSION,
                    "unavailable_required_inputs": ["upstream"],
                }
                output = execute_stage(run_fixture(), stage_id, context)
                self.assertEqual(output["availability"], "unavailable")
                self.assertFalse(output["executed"])
        probes.assert_not_called()
        call.assert_not_called()

    def test_unavailable_output_is_not_valid_evidence(self):
        run = run_fixture(
            {
                "S": {
                    "availability": "unavailable",
                    "candidates": [{"ref": "candidate:fake"}],
                }
            }
        )
        context = build_stage_input(run, "H7")
        self.assertEqual(context["candidates"], [])
        self.assertEqual(context["unavailable_required_inputs"], ["S"])
        self.assertNotIn("stage:S", context["available_refs"])
        self.assertNotIn("candidate:fake", context["available_refs"])

    def test_final_reuses_partial_synthesis_without_extra_paid_call(self):
        context = {
            "kind": "problem",
            "prompt_version": PROMPT_VERSION,
            "missing_evidence": [{"stage_id": "S"}],
            "stages": {
                "R": {"readiness": "READY", "summary": "Partial feedback"},
                "P_FOLLOWUP": {"requested": False},
            },
        }
        with patch("judge.review.coordinator._call") as call:
            output = execute_stage(run_fixture(), "R_FINAL", context)
        self.assertEqual(output["readiness"], "INCOMPLETE")
        self.assertEqual(output["summary"], "Partial feedback")
        call.assert_not_called()

    @patch("judge.review.coordinator.LLMService")
    def test_contradictory_clean_flag_is_conservatively_uncertain(self, service):
        response = section()
        response["uncertainties"] = ["Unresolved important condition"]
        service.return_value.call_llm.return_value = json.dumps(response)
        service.return_value.timeout = 90
        result = _call(
            run_fixture(),
            "H1",
            {
                "kind": "problem",
                "stage_id": "H1",
                "prompt_version": PROMPT_VERSION,
                "available_refs": [],
            },
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["uncertainties"], response["uncertainties"])

    @patch("llm_service.llm_api.fp.get_bot_response_sync", return_value=[])
    @patch("llm_service.llm_api.fp.stream_request")
    @patch("llm_service.llm_api.LLMService._record_usage")
    def test_coordinator_transport_attempts_are_not_hidden_sdk_retries(
        self, record, transport, default_transport
    ):
        service = LLMService(
            api_key="test-only",
            timeout=0,
            transport_attempts=1,
            parameters={"temperature": 0.2},
        )
        transport.return_value = empty_poe_stream()
        service.call_llm("fixture")
        self.assertEqual(transport.call_args.kwargs["num_tries"], 1)
        self.assertTrue(
            all(
                message.parameters == {"temperature": 0.2}
                for message in transport.call_args.kwargs["request"].query
            )
        )
        LLMService(api_key="test-only", timeout=0).call_llm("fixture")
        self.assertNotIn("num_tries", default_transport.call_args.kwargs)

    def setUp(self):
        self.snapshot = {
            "kind": "problem",
            "statement": "Find the sum of N integers.",
            "config": {"time_limit": 1, "memory_limit": 65536, "points": 2900},
            "data_config": {
                "checker": "standard",
                "generator_script": "secret generator",
            },
            "artifacts": {},
            "editorial": {"content": "secret editorial"},
            "tests": [{"id": 5, "type": "C", "generator_args": "secret arguments"}],
            "solutions": [
                {
                    "id": 3,
                    "ref": "solution:1",
                    "name": "secret author name",
                    "source": "print(42)",
                    "language": "PY3",
                    "expected_result": "WA",
                    "evidence": {"status": "D", "result": "AC"},
                }
            ],
        }

    def test_blind_contract_and_solver_never_receive_author_evidence(self):
        run = run_fixture(
            {"S": {"candidates": [{"statement": "secret candidate"}]}}, self.snapshot
        )
        for stage_id in ("E", "FG"):
            context = build_stage_input(run, stage_id)
            encoded = json.dumps(context)
            for secret in (
                "secret editorial",
                "secret author name",
                "secret candidate",
                "secret generator",
                "print(42)",
                "secret arguments",
            ):
                self.assertNotIn(secret, encoded)
            self.assertNotIn("points", context["config"])
            self.assertFalse(
                any(
                    ref.startswith(("solution:", "test:", "artifact:", "instructions:"))
                    for ref in context["available_refs"]
                )
            )

    def test_blind_solution_analysis_receives_code_but_no_labels_results(self):
        context = build_stage_input(run_fixture(snapshot=self.snapshot), "H2_BLIND")
        self.assertEqual(
            context["solutions"],
            [{"ref": "solution:1", "source": "print(42)", "language": "PY3"}],
        )
        encoded = json.dumps(context)
        self.assertNotIn("secret author name", encoded)
        self.assertNotIn("expected_result", encoded)
        self.assertNotIn("secret editorial", encoded)

    def test_reconciliation_receives_saved_blind_analysis(self):
        context = build_stage_input(
            run_fixture(
                {"H2_BLIND": {"summary": "Saved independent analysis"}}, self.snapshot
            ),
            "H2",
        )
        self.assertEqual(
            context["blind_analysis"]["summary"], "Saved independent analysis"
        )
        self.assertEqual(context["solutions"][0]["name"], "secret author name")

    def test_dag_every_dependency_exists_and_is_acyclic(self):
        for kind in ("problem", "contest"):
            definitions = stage_definitions(kind)
            seen = set()
            for name, definition in definitions.items():
                self.assertTrue(set(definition["dependencies"]).issubset(seen), name)
                seen.add(name)

    def test_json_response_requires_evidence_refs_for_findings(self):
        finding = {
            "status": "finding",
            "severity": "blocking",
            "confidence": "high",
            "claim": "Wrong",
            "reasoning": "Counterexample",
            "evidence_refs": [],
            "execution_confirmation": False,
            "suggested_probe_or_fix": "Fix it",
            "needs_human": False,
        }
        with self.assertRaises(CoordinatorOutputError):
            validate_output(
                section("finding", [finding]),
                "H1",
                {"kind": "problem", "available_refs": ["statement"]},
            )
        finding["evidence_refs"] = ["invented_test"]
        with self.assertRaises(CoordinatorOutputError):
            validate_output(
                section("finding", [finding]),
                "H1",
                {"kind": "problem", "available_refs": ["statement"]},
            )

    def test_specialist_cannot_return_global_ready(self):
        output = section()
        output["readiness"] = "READY"
        with self.assertRaises(CoordinatorOutputError):
            validate_output(output, "H1", {"kind": "problem", "available_refs": []})

    def test_probe_positions_obey_native_protocol_bounds(self):
        output = section()
        probe = {
            "id": "p1",
            "input": "1\n",
            "expected_output": "",
            "candidate_output": "",
            "expected_claim": "accept",
            "rationale": "Legal boundary",
            "evidence_refs": ["statement"],
            "batch": 0,
            "position": 1000000,
        }
        output["probes"] = [probe]
        context = {"kind": "problem", "available_refs": ["statement"]}
        validate_output(output, "H4_PLAN", context)
        for invalid in (-1, 1000001, True, "0"):
            probe["position"] = invalid
            with self.subTest(position=invalid), self.assertRaises(
                CoordinatorOutputError
            ):
                validate_output(output, "H4_PLAN", context)

    def test_h7_must_compare_all_candidates_and_reject_fake_percentages(self):
        output = section()
        output["comparisons"] = []
        context = {
            "kind": "problem",
            "available_refs": ["candidate:5"],
            "candidates": [{"ref": "candidate:5"}],
        }
        with self.assertRaises(CoordinatorOutputError):
            validate_output(output, "H7", context)

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    def test_each_call_is_single_stateless_provider_request(
        self, service_class, get_config
    ):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            timeout=120,
            get_bot_name_for_review=lambda: "test-model",
        )
        service_class.return_value.call_llm.return_value = json.dumps(section())
        service_class.return_value.timeout = 120
        context = {
            "kind": "problem",
            "available_refs": [],
            "prompt_version": PROMPT_VERSION,
        }
        result = _call(run_fixture(), "H1", context)
        self.assertEqual(result["status"], "no_issue")
        self.assertEqual(service_class.return_value.call_llm.call_count, 1)
        self.assertEqual(service_class.call_args.kwargs["parameters"], {})
        self.assertEqual(result["_request"]["model"], "test-model")
        prompt = json.loads(service_class.return_value.call_llm.call_args.args[0])
        self.assertEqual(prompt["allowed_evidence_reference_ids"], [])
        system_prompt = service_class.return_value.call_llm.call_args.kwargs[
            "system_prompt"
        ]
        self.assertEqual(system_prompt, SYSTEM)
        self.assertIn("tiếng Việt tự nhiên", " ".join(system_prompt.split()))
        self.assertIn("tiêu đề nhận xét", system_prompt)
        self.assertIn("góp ý cho đồng nghiệp", system_prompt)
        self.assertIn("Validator chưa từ chối giá trị âm", system_prompt)
        self.assertIn("giữ nguyên enum trong JSON", system_prompt)
        self.assertIn(
            "Markdown is allowed INSIDE narrative JSON string values", system_prompt
        )
        self.assertIn(r"$N, Q \le 200\,000$", system_prompt)
        self.assertIn("evidence_refs/source_refs", system_prompt)
        self.assertIn("READER/INTERNAL SEPARATION", system_prompt)
        self.assertIn("exactly FOUR spaces per nesting level", system_prompt)
        self.assertIn("populate it", system_prompt)
        self.assertIn("author_feedback", prompt["required_output_schema"]["properties"])
        self.assertEqual(
            prompt["statement_format_requirements"], PROBLEM_STATEMENT_FORMAT_RULES
        )
        self.assertEqual(prompt["markdown_formatting_rules"], MARKDOWN_FORMATTING_RULES)
        self.assertIn("warning findings", prompt["stage_scope"])
        self.assertIn("nonblocking advice", prompt["stage_scope"])
        self.assertIn("affected excerpt", prompt["stage_scope"])

    @patch("ai_features.markdown_improver.LLMService")
    def test_markdown_improver_uses_the_same_statement_format_requirements(
        self, service
    ):
        service.return_value.call_llm_with_files.return_value = (
            "#### Input\n\n- Read $N$."
        )
        improver = MarkdownImprover(api_key="test-key")
        result = improver.improve_markdown("### Dữ liệu vào\nRead N.")
        self.assertTrue(result["success"])
        prompt = service.return_value.call_llm_with_files.call_args.args[2]
        self.assertIn(PROBLEM_STATEMENT_FORMAT_RULES, prompt)
        self.assertIn(get_markdown_rules_for_prompt(start_number=9), prompt)

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    def test_statement_format_rules_do_not_leak_into_other_specialists(
        self, service, get_config
    ):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            get_bot_name_for_review=lambda: "test-model",
            timeout=120,
        )
        service.return_value.call_llm.return_value = json.dumps(section())
        for kind, stage in (("problem", "H4"), ("contest", "H1")):
            with self.subTest(kind=kind, stage=stage):
                _call(run_fixture(), stage, {"kind": kind, "available_refs": []})
                prompt = json.loads(service.return_value.call_llm.call_args.args[0])
                self.assertNotIn("statement_format_requirements", prompt)
                self.assertNotIn("markdown_formatting_rules", prompt)

    def test_reader_feedback_has_a_validated_separate_output_field(self):
        output = {
            **section(),
            "author_feedback": "Các test đã kiểm tra đều hợp lệ.",
        }
        context = {"kind": "problem", "available_refs": []}
        self.assertEqual(validate_output(output, "H4", context), output)
        with self.assertRaises(CoordinatorOutputError):
            validate_output({**output, "author_feedback": []}, "H4", context)

    def test_previous_prompt_versions_remain_supported_for_frozen_stages(self):
        for version in (
            "cp-coordinator-1",
            "cp-coordinator-2",
            "cp-coordinator-3",
            "cp-coordinator-4",
            "cp-coordinator-5",
            "cp-coordinator-6",
            "cp-coordinator-7",
            "cp-coordinator-8",
            "cp-coordinator-9",
            "cp-coordinator-10",
            "cp-coordinator-11",
            "cp-coordinator-12",
            PROMPT_VERSION,
        ):
            with self.subTest(version=version):
                result = execute_stage(
                    run_fixture(),
                    "H1",
                    {
                        "prompt_version": version,
                        "unavailable_required_inputs": ["stage:E"],
                    },
                )
                self.assertFalse(result["executed"])

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    def test_contest_specialists_and_synthesis_receive_natural_writing_guidance(
        self, service, get_config
    ):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            get_bot_name_for_review=lambda: "test-model",
            timeout=120,
        )
        service.return_value.call_llm.return_value = json.dumps(section())
        for stage in ("H1", "H2", "R"):
            with self.subTest(stage=stage):
                service.return_value.call_llm.return_value = json.dumps(
                    {
                        "readiness": "READY",
                        "summary": "Chưa phát hiện lỗi.",
                        "findings": [],
                        "uncertainties": [],
                        "actions": [],
                        "follow_up_requests": [],
                    }
                    if stage == "R"
                    else section()
                )
                _call(run_fixture(), stage, {"kind": "contest", "available_refs": []})
                prompt = json.loads(service.return_value.call_llm.call_args.args[0])
                self.assertEqual(prompt["reader_writing_rules"], CONTEST_WRITING)
                self.assertIn('"kỳ thi"', prompt["reader_writing_rules"])
                self.assertIn(
                    'không\nkhẳng định "đã lộ đề"', prompt["reader_writing_rules"]
                )
        service.return_value.call_llm.return_value = json.dumps(section())
        _call(run_fixture(), "H4", {"kind": "problem", "available_refs": []})
        prompt = json.loads(service.return_value.call_llm.call_args.args[0])
        self.assertNotIn("reader_writing_rules", prompt)

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    @patch("judge.review.coordinator.preparation_errors", return_value=[])
    def test_new_synthesis_excludes_history_but_keeps_current_run_evidence(
        self, preparation, service, get_config
    ):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            timeout=120,
            get_bot_name_for_review=lambda: "test-model",
        )
        service.return_value.call_llm.return_value = json.dumps(
            {
                "readiness": "READY",
                "summary": "Current package assessed",
                "findings": [],
                "uncertainties": [],
                "actions": [],
                "follow_up_requests": [],
            }
        )
        history = {
            "summary": "OUTDATED_FORMAT_WARNING",
            "findings": [{"claim": "OUTDATED_FORMAT_WARNING"}],
            "actions": ["OUTDATED_FORMAT_WARNING"],
            "readiness": "READY_WITH_WARNINGS",
        }
        current_statement = "#### Input\n#### Output\n#### Example\n!!! question"
        current_review = {**section(), "summary": "CURRENT_FORMAT_CONFIRMED"}
        current_execution = {"available": True, "jobs": [{"state": "done"}]}
        for kind in ("problem", "contest"):
            with self.subTest(kind=kind):
                run = run_fixture(
                    snapshot={
                        "kind": kind,
                        "statement": current_statement,
                        "previous_assessment": history,
                    },
                    outputs={"H1": current_review, "P_CHECKER": current_execution},
                )
                context = build_stage_input(run, "Q")
                self.assertNotIn("previous_assessment", context)
                dossier = execute_stage(run, "Q", context)
                self.assertNotIn("previous_assessment", dossier)
                # Old persisted input must not reintroduce history into a new dossier.
                self.assertNotIn(
                    "previous_assessment",
                    _dossier({**context, "previous_assessment": history}),
                )
                run.coordinator_stages.all.return_value.append(
                    SimpleNamespace(stage_id="Q", status="D", output_json=dossier)
                )
                synthesis = build_stage_input(run, "R")
                _call(run, "R", synthesis)
                prompt = service.return_value.call_llm.call_args.args[0]
                self.assertNotIn("previous_assessment", prompt)
                self.assertNotIn("OUTDATED_FORMAT_WARNING", prompt)
                self.assertIn("CURRENT_FORMAT_CONFIRMED", prompt)
                synthesis_dossier = (
                    synthesis["dossier"]
                    if kind == "contest"
                    else synthesis["stages"]["Q"]
                )
                self.assertEqual(
                    synthesis_dossier["stages"]["P_CHECKER"], current_execution
                )
                self.assertEqual(run.snapshot["previous_assessment"], history)

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    def test_malformed_response_has_no_hidden_retry(self, service_class, get_config):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            timeout=120,
            get_bot_name_for_review=lambda: "test-model",
        )
        service_class.return_value.call_llm.return_value = "not JSON"
        with self.assertRaises(CoordinatorOutputError) as caught:
            _call(
                run_fixture(),
                "H1",
                {
                    "kind": "problem",
                    "available_refs": [],
                    "prompt_version": PROMPT_VERSION,
                },
            )
        self.assertEqual(caught.exception.raw_response, "not JSON")
        self.assertEqual(service_class.return_value.call_llm.call_count, 1)

    def test_provider_failure_does_not_add_model_response_correction(self):
        stage = SimpleNamespace(error_category="provider", attempt_records=MagicMock())
        self.assertIsNone(_response_correction(stage, "H1", {"kind": "problem"}))
        stage.attempt_records.filter.assert_not_called()

    def test_invalid_json_retry_never_replays_failed_text(self):
        attempts = MagicMock()
        attempts.filter.return_value.exclude.return_value.order_by.return_value.first.return_value = SimpleNamespace(
            raw_response="BAD_RESPONSE_WITH_UNTRUSTED_COMMAND"
        )
        stage = SimpleNamespace(
            error_category="invalid_response", attempts=2, attempt_records=attempts
        )
        feedback = _response_correction(stage, "H1", {"kind": "problem"})
        self.assertEqual(feedback["validation_error"]["code"], "invalid_json")
        self.assertNotIn("UNTRUSTED_COMMAND", json.dumps(feedback))
        instruction = feedback["validation_error"]["instruction"]
        self.assertIn("literal JSON strings", instruction)
        self.assertIn("omit that probe", instruction)
        self.assertIn("source-backed finding", instruction)

    def test_invalid_latex_json_retry_identifies_escaping_without_replaying_prose(self):
        attempts = MagicMock()
        attempts.filter.return_value.exclude.return_value.order_by.return_value.first.return_value = SimpleNamespace(
            raw_response=r'{"summary":"UNTRUSTED_COMMAND $\{1,2\}$"}'
        )
        stage = SimpleNamespace(
            error_category="invalid_response", attempts=2, attempt_records=attempts
        )
        correction = _response_correction(stage, "H7", {"kind": "problem"})
        error = correction["validation_error"]
        self.assertEqual(error["code"], "invalid_json")
        self.assertEqual(error["line"], 1)
        self.assertGreater(error["column"], 1)
        self.assertIn("EVERY LaTeX backslash", error["json_escaping_instruction"])
        self.assertNotIn("UNTRUSTED_COMMAND", json.dumps(correction))

    @patch("judge.review.coordinator.get_config")
    @patch("judge.review.coordinator.LLMService")
    def test_probe_planning_requires_literal_bounded_inputs(
        self, service_class, get_config
    ):
        get_config.return_value = SimpleNamespace(
            api_key="test-key",
            timeout=120,
            get_bot_name_for_review=lambda: "test-model",
        )
        service_class.return_value.call_llm.return_value = json.dumps(
            {
                "status": "uncertain",
                "summary": "Large boundary needs execution.",
                "author_feedback": "Boundary defect found in source.",
                "findings": [],
                "uncertainties": ["Large input omitted."],
                "probes": [],
            }
        )
        _call(
            run_fixture(),
            "H4_PLAN",
            {
                "kind": "problem",
                "available_refs": [],
                "prompt_version": PROMPT_VERSION,
            },
        )
        prompt = json.loads(service_class.return_value.call_llm.call_args.args[0])
        instruction = prompt["probe_serialization_instruction"]
        self.assertIn(
            "Escape every backslash", prompt["json_serialization_instruction"]
        )
        self.assertIn("complete literal text", instruction)
        self.assertIn("128000", instruction)
        self.assertIn("omit that probe", instruction)
        self.assertIn("not execution confirmation", instruction)

    @patch("judge.review.coordinator.execution.execute_coordinator_evidence")
    def test_probe_waiting_preserves_pending_not_semantic_success(self, execute):
        execute.return_value = {"state": "pending", "job_id": "job1"}
        context = {
            "kind": "problem",
            "prompt_version": PROMPT_VERSION,
            "validator": {
                "source": "print(1)",
                "sha256": hashlib.sha256(b"print(1)").hexdigest(),
            },
            "language": "python",
            "proposed_assessment": {
                "probes": [{"id": "p1", "input": "1", "expected_claim": "accept"}]
            },
        }
        with self.assertRaises(ReviewExecutionPending):
            execute_stage(run_fixture(), "P_VALIDATOR", context)

    @patch("judge.review.coordinator.execution.execute_coordinator_evidence")
    def test_worker_failure_is_infrastructure_not_author_defect(self, execute):
        execute.return_value = {"state": "unavailable", "infrastructure": True}
        context = {
            "kind": "problem",
            "prompt_version": PROMPT_VERSION,
            "validator": {"source": "", "sha256": hashlib.sha256(b"").hexdigest()},
            "language": "python",
            "proposed_assessment": {"probes": [{"id": "p1", "input": "1"}]},
        }
        with self.assertRaises(CoordinatorInfrastructureError):
            execute_stage(run_fixture(), "P_VALIDATOR", context)

    def test_final_synthesis_no_followup_does_not_make_another_call(self):
        context = {
            "kind": "problem",
            "prompt_version": PROMPT_VERSION,
            "stages": {
                "R": {"readiness": "MANUAL_REVIEW"},
                "P_FOLLOWUP": {"requested": False},
            },
        }
        with patch("judge.review.coordinator._call") as call:
            result = execute_stage(run_fixture(), "R_FINAL", context)
        self.assertEqual(result["readiness"], "MANUAL_REVIEW")
        call.assert_not_called()

    def test_bounded_per_case_context_records_omissions(self):
        cases = [{"case": index, "status": "AC", "time": index} for index in range(500)]
        result = _bounded_cases(cases, 30)
        self.assertEqual(result["total"], 500)
        self.assertEqual(result["omitted"], 470)
        self.assertEqual(len(result["records"]), 30)
        self.assertIn(cases[-1], result["records"])

    def test_extractor_repair_only_for_actual_code_failures(self):
        self.assertFalse(
            _extract_failed(
                {"available": False, "unprocessed": ["Generated inputs unavailable"]}
            )
        )
        self.assertTrue(
            _extract_failed({"jobs": [{"result": {"compile_error": "Syntax error"}}]})
        )

    def test_extraction_summary_retains_counts_ranges_and_omissions(self):
        cases = [
            {
                "id": str(index),
                "status": "EXIT_ZERO",
                "time": index,
                "statistics": {"parse_status": "ok", "statistics": {"N": index}},
                "stdout": "private verbose data",
            }
            for index in range(150)
        ]
        result = _summarize_extraction(
            {
                "jobs": [
                    {
                        "state": "done",
                        "job_id": "job1",
                        "result": {"processed": 150, "failed": 0, "cases": cases},
                    }
                ],
                "unprocessed": [],
                "limitations": [],
            }
        )
        self.assertEqual(result["processed"], 150)
        self.assertEqual(result["metrics"]["N"]["maximum"], 149)
        self.assertEqual(result["per_file"]["omitted"], 70)
        self.assertNotIn("private verbose data", json.dumps(result))

    def test_custom_checker_requires_real_acceptance_and_rejection(self):
        cases = [
            {"id": "valid", "status": "EXIT_ZERO", "checker_result": {"passed": True}},
            {
                "id": "invalid",
                "status": "EXIT_NONZERO",
                "checker_result": {"passed": False},
            },
        ]
        observations = {
            "available": True,
            "probes": [
                {"id": "valid", "expected_claim": "accept"},
                {"id": "invalid", "expected_claim": "reject"},
            ],
            "jobs": [{"result": {"cases": cases}}],
        }
        self.assertTrue(_custom_checker_challenges(observations))
        cases[1]["status"] = "TLE"
        self.assertFalse(_custom_checker_challenges(observations))
        self.assertFalse(_custom_checker_challenges({"available": True, "jobs": []}))


class CoordinatorReadinessTests(SimpleTestCase):
    def test_unsupported_child_missing_batch_report_is_manual(self):
        outputs = self.complete("contest")
        outputs["E"] = {
            "children": [
                {"unsupported": True, "fresh": False, "readiness": "INCOMPLETE"}
            ]
        }
        self.assertEqual(
            effective_readiness(run_fixture(outputs, {"kind": "contest"})),
            "MANUAL_REVIEW",
        )

    def test_visual_statement_cannot_be_certified_by_text_only_analysis(self):
        self.assertEqual(
            effective_readiness(
                run_fixture(
                    self.complete(),
                    {"kind": "problem", "media_limitations": ["visual input"]},
                )
            ),
            "MANUAL_REVIEW",
        )

    def complete(self, kind="problem"):
        outputs = {name: {} for name in stage_definitions(kind)}
        outputs["D"] = {"complete": True}
        outputs["R_FINAL" if kind == "problem" else "R"] = {"readiness": "READY"}
        return outputs

    def test_missing_any_required_section_never_ready(self):
        outputs = self.complete()
        for name in outputs:
            changed = copy.deepcopy(outputs)
            del changed[name]
            self.assertEqual(
                effective_readiness(run_fixture(changed)), "INCOMPLETE", name
            )

    def test_essential_equivalence_enforces_manual_despite_ai_ready(self):
        outputs = self.complete()
        outputs["H7"] = {"comparisons": [{"relationship": "essentially_equivalent"}]}
        self.assertEqual(effective_readiness(run_fixture(outputs)), "MANUAL_REVIEW")

    def test_duplicate_policy_cannot_be_reclassified_as_author_defect(self):
        outputs = self.complete()
        outputs["H7"] = {
            "comparisons": [{"relationship": "essentially_equivalent"}],
            "findings": [
                {"status": "finding", "severity": "blocking", "needs_human": False}
            ],
        }
        outputs["R_FINAL"] = {"readiness": "NEEDS_AUTHOR_FIX"}
        self.assertEqual(effective_readiness(run_fixture(outputs)), "MANUAL_REVIEW")
        outputs["H1"] = {
            "findings": [
                {"status": "finding", "severity": "blocking", "needs_human": False}
            ]
        }
        self.assertEqual(effective_readiness(run_fixture(outputs)), "NEEDS_AUTHOR_FIX")

    def test_custom_checker_without_actual_challenges_never_ready(self):
        outputs = self.complete()
        self.assertEqual(
            effective_readiness(
                run_fixture(
                    outputs, {"kind": "problem", "data_config": {"checker": "testlib"}}
                )
            ),
            "MANUAL_REVIEW",
        )

    def test_unsupported_quiz_is_manual_not_infra_failure(self):
        outputs = self.complete("contest")
        outputs["E"] = {"items": [{"quiz_id": 10}], "children": []}
        self.assertEqual(
            effective_readiness(run_fixture(outputs, {"kind": "contest"})),
            "MANUAL_REVIEW",
        )

    def test_stale_child_cannot_be_certified_by_good_contest_composition(self):
        outputs = self.complete("contest")
        outputs["E"] = {
            "items": [],
            "children": [{"fresh": False, "readiness": "READY"}],
        }
        self.assertEqual(
            effective_readiness(run_fixture(outputs, {"kind": "contest"})), "INCOMPLETE"
        )


class CoordinatorSnapshotTests(SimpleTestCase):
    def review_snapshot(self):
        return {
            "kind": "problem",
            "statement": "Task\n\n- Rule\n    - Child\n![diagram](diagram.svg)",
            "name": "Old title",
            "config": {
                "time_limit": 2,
                "memory_limit": 1024,
                "points": 1500,
                "date": None,
            },
            "data_config": {"checker": "standard"},
            "tests": [{"id": 1, "type": "C", "input_file": "1.in", "points": 1}],
            "artifacts": {
                "generator": {"filename": "gen.cpp", "sha256": "a", "size": 1}
            },
            "roles": {
                "authors": [1],
                "curators": [],
                "testers": [],
                "organizations": [],
                "allowed_languages": [12],
            },
            "solutions": [
                {
                    "id": 1,
                    "ref": "solution:1",
                    "name": "main",
                    "source": "code",
                    "language": "CPP17",
                    "language_id": 12,
                    "order": 0,
                    "expected_result": "AC",
                    "evidence": {"id": 1},
                },
                {
                    "id": 2,
                    "ref": "solution:2",
                    "name": "slow",
                    "source": "brute",
                    "language": "CPP17",
                    "order": 1,
                    "expected_result": "TLE",
                },
            ],
            "editorial": {"content": "Proof\n", "is_public": False},
            "translations": [
                {"language": "en", "name": "Title", "description": "Task\n"}
            ],
            "language_limits": [
                {"id": 1, "problem_id": 2, "language_id": 12, "time_limit": 2}
            ],
            "attachments": [
                {
                    "id": 1,
                    "filename": "diagram.svg",
                    "description": "Diagram",
                    "order": 1,
                    "artifact": {"sha256": "a", "size": 1},
                },
                {
                    "id": 2,
                    "filename": "unrelated.txt",
                    "artifact": {"sha256": "b", "size": 1},
                },
            ],
            "validation": {"id": 1},
            "test_data_instructions": {"sha256": "docs"},
        }

    def test_review_identity_ignores_admin_metadata_ids_display_order_and_newlines(
        self,
    ):
        original = self.review_snapshot()
        changed = copy.deepcopy(original)
        changed["name"] = "Renamed"
        changed["config"].update(
            date="today", group_id=99, license_id=2, summary="new", og_image="new"
        )
        changed["roles"].update(
            authors=[2], curators=[3], testers=[4], organizations=[5]
        )
        changed["statement"] = changed["statement"].replace("\n", "\r\n")
        changed["editorial"].update(is_public=True, content="Proof\r\n")
        changed["translations"][0].update(name="New title", description="Task\r\n")
        changed["tests"][0]["id"] = 500
        changed["language_limits"][0].update(id=100, problem_id=500)
        changed["solutions"].reverse()
        changed["solutions"][0]["name"] = "Renamed display label"
        for index, code in enumerate(changed["solutions"]):
            code.update(
                id=100 + index, ref="solution:new", order=index, language_id=100
            )
        changed["attachments"][0].update(id=300, order=20)
        changed["attachments"][1]["artifact"]["sha256"] = "unrelated change"
        changed["artifacts"]["generator"]["filename"] = "renamed.cpp"
        self.assertEqual(
            problem_review_content(original), problem_review_content(changed)
        )
        self.assertEqual(
            problem_review_input_hash(original), problem_review_input_hash(changed)
        )
        legacy = SimpleNamespace(snapshot=original, content_hash="historical-hash")
        self.assertEqual(
            reviewed_content_hash(legacy), digest_json(problem_review_content(changed))
        )
        self.assertEqual(legacy.content_hash, "historical-hash")

    def test_review_identity_detects_technical_changes(self):
        original = self.review_snapshot()
        mutations = [
            (
                "statement",
                lambda s: s.update(
                    statement=s["statement"].replace("    - Child", "- Child")
                ),
            ),
            ("limit", lambda s: s["config"].update(time_limit=3)),
            ("difficulty", lambda s: s["config"].update(points=2000)),
            ("checker", lambda s: s["data_config"].update(checker="floats")),
            ("test scoring", lambda s: s["tests"][0].update(points=2)),
            ("source", lambda s: s["solutions"][0].update(source="changed")),
            (
                "expected verdict",
                lambda s: s["solutions"][0].update(expected_result="WA"),
            ),
            (
                "starter template",
                lambda s: s.update(
                    language_templates=[
                        {"language": "CPP17", "source": "int main() {}"}
                    ]
                ),
            ),
            ("editorial", lambda s: s["editorial"].update(content="Different proof")),
            (
                "translation",
                lambda s: s["translations"][0].update(description="Different rule"),
            ),
            ("language", lambda s: s["roles"].update(allowed_languages=[13])),
            ("language limit", lambda s: s["language_limits"][0].update(time_limit=3)),
            (
                "artifact bytes",
                lambda s: s["artifacts"]["generator"].update(sha256="changed"),
            ),
            (
                "artifact runtime extension",
                lambda s: s["artifacts"]["generator"].update(filename="gen.py"),
            ),
            (
                "PDF statement",
                lambda s: s["artifacts"].update(
                    pdf_description={
                        "sha256": "pdf",
                        "size": 20,
                        "filename": "statement.pdf",
                    }
                ),
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(original)
                mutate(changed)
                self.assertNotEqual(
                    problem_review_content(original), problem_review_content(changed)
                )

    def test_review_execution_evidence_is_separate_and_still_required(self):
        original = self.review_snapshot()
        for field in ("validation",):
            changed = copy.deepcopy(original)
            changed[field] = {"status": "D", "passed": False, "total_cases": 1}
            self.assertEqual(
                problem_review_content(original), problem_review_content(changed)
            )
            self.assertNotEqual(
                problem_review_input_hash(original), problem_review_input_hash(changed)
            )
        changed = copy.deepcopy(original)
        changed["solutions"][0]["evidence"] = {"status": "D", "result": "WA"}
        self.assertEqual(
            problem_review_content(original), problem_review_content(changed)
        )
        self.assertNotEqual(
            problem_review_input_hash(original), problem_review_input_hash(changed)
        )

    def test_execution_identity_ignores_statement_and_publication_date(self):
        snapshot = {
            "config": {"date": None, "time_limit": 2},
            "data_config": {"checker": "standard"},
            "tests": [{"type": "C", "input_file": "1.in"}],
            "artifacts": {
                "testcase_validator": {
                    "sha256": "validator",
                    "size": 1,
                    "filename": "validator.cpp",
                }
            },
            "language_limits": [],
            "statement": "N <= 200000",
        }
        original = execution_package_identity(snapshot)
        snapshot["statement"] = "N <= 300000"
        snapshot["config"]["date"] = "2026-10-17T07:44:39+00:00"
        self.assertEqual(execution_package_identity(snapshot), original)
        for field in ("config", "data_config", "tests", "artifacts", "language_limits"):
            changed = copy.deepcopy(snapshot)
            if isinstance(changed[field], dict):
                changed[field]["time_limit" if field == "config" else "extra"] = None
                if field == "artifacts":
                    changed[field]["testcase_validator"]["sha256"] = "changed"
            else:
                changed[field].append({"changed": True})
            self.assertNotEqual(execution_package_identity(changed), original, field)

    def test_legacy_execution_evidence_survives_first_save_but_not_package_changes(
        self,
    ):
        snapshot = {
            "config": {"date": None, "time_limit": 2},
            "data_config": {"checker": "standard"},
            "tests": [{"type": "C", "input_file": "1.in"}],
            "artifacts": {},
            "language_limits": [],
        }
        # Reproduce the exact old hash, which included a null publication date.
        legacy = execution_package_identity(snapshot)
        legacy["config"]["date"] = None
        evidence = {
            "review_evidence": {
                "package_hash": digest_json(legacy),
                "source_hash": "solution",
                "expected_cases": 1,
                "environment": {"judge": "test"},
            }
        }
        snapshot["config"]["date"] = "2026-10-17T07:44:39+00:00"
        snapshot["package_hash"] = digest_json(execution_package_identity(snapshot))
        self.assertTrue(_bound_evidence(evidence, snapshot, "solution"))
        self.assertFalse(_bound_evidence(evidence, snapshot, "different source"))
        for field in ("config", "data_config", "tests", "artifacts", "language_limits"):
            changed = copy.deepcopy(snapshot)
            if field == "config":
                changed[field]["time_limit"] = 3
            elif field == "data_config":
                changed[field]["checker"] = "identical"
            elif field == "artifacts":
                changed[field]["generator"] = {
                    "sha256": "new",
                    "size": 1,
                    "filename": "gen.py",
                }
            else:
                changed[field].append({"changed": True})
            changed["package_hash"] = digest_json(execution_package_identity(changed))
            self.assertFalse(_bound_evidence(evidence, changed, "solution"), field)

    def test_attachment_overwrite_changes_identity_without_storage_path_dependency(
        self,
    ):
        first = [
            {
                "id": 1,
                "filename": "diagram.svg",
                "artifact": {
                    "sha256": "a",
                    "size": 5,
                    "filename": "diagram.svg",
                    "storage_name": "private/a",
                },
            }
        ]
        second = copy.deepcopy(first)
        second[0]["artifact"]["sha256"] = "b"
        self.assertNotEqual(
            attachment_content_identity(first), attachment_content_identity(second)
        )
        second = copy.deepcopy(first)
        second[0]["artifact"]["storage_name"] = "private/other"
        self.assertEqual(
            attachment_content_identity(first), attachment_content_identity(second)
        )

    def test_actual_input_manifest_distinguishes_nondeterministic_generated_bytes(self):
        evidence = {
            "review_evidence": {
                "worker_attestation": {
                    "case_manifest": [{"case": 0, "input_sha256": "a"}]
                }
            }
        }
        changed = copy.deepcopy(evidence)
        changed["review_evidence"]["worker_attestation"]["case_manifest"][0][
            "input_sha256"
        ] = "b"
        self.assertNotEqual(case_input_manifest(evidence), case_input_manifest(changed))
        flat = copy.deepcopy(evidence)
        flat["review_evidence"]["worker_attestation"]["case_manifest"][0][
            "batch"
        ] = None
        self.assertEqual(case_input_manifest(evidence), case_input_manifest(flat))

    def test_subtask_input_manifests_require_matching_batch_and_global_position(self):
        def evidence(batch, position):
            return {
                "review_evidence": {
                    "worker_attestation": {
                        "case_manifest": [
                            {
                                "batch": batch,
                                "case": position,
                                "input_sha256": "same-bytes",
                            }
                        ]
                    }
                }
            }

        validator = evidence(2, 7)
        self.assertEqual(
            case_input_manifest(validator), case_input_manifest(evidence(2, 7))
        )
        self.assertNotEqual(
            case_input_manifest(validator), case_input_manifest(evidence(None, 7))
        )
        self.assertNotEqual(
            case_input_manifest(validator), case_input_manifest(evidence(1, 7))
        )
        self.assertNotEqual(
            case_input_manifest(validator), case_input_manifest(evidence(2, 1))
        )

    def test_verified_selected_cpp_runtime_beats_available_newer_runtime(self):
        snapshot = {
            "validation": {
                "review_evidence": {
                    "environment": {
                        "helper_runtime": "CPP20",
                        "runtimes": {"CPP20": [], "CPP23": []},
                    }
                }
            }
        }
        self.assertEqual(
            _helper_language(snapshot, "validator_probe", {"filename": "v.cpp"})[0],
            "CPP20",
        )
        snapshot["validation"]["review_evidence"]["environment"].pop("helper_runtime")
        self.assertEqual(
            _helper_language(snapshot, "validator_probe", {"filename": "v.cpp"})[0],
            "CPP23",
        )

    def test_missing_manifest_artifact_is_explicit_not_positive_evidence(self):
        missing = MagicMock(name="file")
        missing.name = "missing/tests.zip"
        missing.open.side_effect = FileNotFoundError
        marker = _manifest_artifact(missing, persist=False, strict=False)
        self.assertTrue(marker["unavailable"])
        self.assertIsNone(marker["sha256"])
        with self.assertRaises(FileNotFoundError):
            _manifest_artifact(missing, persist=True, strict=True)

    def test_cpp_helper_matches_frozen_native_runtime_preference(self):
        snapshot = {
            "validation": {
                "review_evidence": {
                    "environment": {"runtimes": {"CPP17": [], "CPP20": []}}
                }
            }
        }
        language, assurance = _helper_language(
            snapshot, "validator_probe", {"filename": "validator.cpp"}
        )
        self.assertEqual(language, "CPP20")
        self.assertTrue(assurance["confirmed"])
        self.assertFalse(
            _helper_language({}, "checker_probe", {"filename": "checker.cpp"})[1][
                "confirmed"
            ]
        )

    def test_child_content_identity_ignores_attachment_order_and_duplicate_rows(self):
        first = {"problem_id": 9, "content_hash": "a", "review_run_id": 2}
        second = {"problem_id": 2, "content_hash": "b", "review_run_id": 3}
        self.assertEqual(
            canonical_child_content([first, second, first]),
            canonical_child_content([second, first]),
        )

    def test_auxiliary_headers_are_frozen_and_passed_with_main_source(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            REVIEW_ARTIFACT_ROOT=directory, MEDIA_ROOT=directory + "/public"
        ):
            snapshot = {
                "artifacts": {
                    "testcase_validator": freeze_file(
                        ContentFile(b'#include "testlib.h"', name="validator.cpp")
                    ),
                    "aux:testlib.h": freeze_file(
                        ContentFile(b"// frozen library", name="testlib.h")
                    ),
                }
            }
            source = _source(snapshot, "testcase_validator")
            self.assertEqual(
                source["aux_sources"]["testlib.h"]["source"], "// frozen library"
            )

    @patch("judge.review.coordinator.execution.prepare_extractor_payloads")
    @patch("judge.review.coordinator._execute_jobs")
    def test_initial_extractor_requires_semantically_verified_fixtures(
        self, jobs, prepare
    ):
        jobs.return_value = {
            "jobs": [
                {
                    "result": {
                        "cases": [
                            {
                                "id": "small",
                                "status": "EXIT_ZERO",
                                "statistics": {
                                    "parse_status": "ok",
                                    "statistics": {"N": 7},
                                },
                            }
                        ]
                    }
                }
            ]
        }
        result = _extract(
            run_fixture(),
            {
                "contract": {
                    "extractor": {"source": "print(0)", "language": "PY3"},
                    "extractor_fixtures": [
                        {"id": "small", "input": "3", "expected_statistics": {"N": 3}}
                    ],
                }
            },
        )
        self.assertFalse(result["available"])
        self.assertTrue(_extract_failed(result))
        prepare.assert_not_called()

    def test_private_content_addressed_bytes_survive_original_replacement(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            REVIEW_ARTIFACT_ROOT=directory, MEDIA_ROOT=directory + "/public"
        ):
            original = ContentFile(b"original validator", name="validator.py")
            artifact = freeze_file(original)
            self.assertEqual(
                os.stat(os.path.join(directory, artifact["storage_name"])).st_mode
                & 0o777,
                0o600,
            )
            self.assertEqual(
                os.stat(
                    os.path.dirname(os.path.join(directory, artifact["storage_name"]))
                ).st_mode
                & 0o777,
                0o700,
            )
            original = ContentFile(b"different validator", name="validator.py")
            self.assertEqual(read_artifact(artifact), b"original validator")
            self.assertNotEqual(freeze_file(original)["sha256"], artifact["sha256"])

    def test_artifact_digest_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            REVIEW_ARTIFACT_ROOT=directory, MEDIA_ROOT=directory + "/public"
        ):
            artifact = freeze_file(ContentFile(b"data", name="test.in"))
            artifact["sha256"] = "0" * 64
            with self.assertRaises(SnapshotError):
                read_artifact(artifact)

    def test_public_media_storage_and_path_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            REVIEW_ARTIFACT_ROOT=directory + "/private", MEDIA_ROOT=directory
        ):
            with self.assertRaises(SnapshotError):
                freeze_file(ContentFile(b"secret", name="private.in"))
        with self.assertRaises(SnapshotError):
            read_artifact(
                {
                    "backend": "review-private",
                    "storage_name": "sha256/../../secret",
                    "size": 0,
                }
            )

    def test_zip_inventory_reads_small_complete_cases_not_just_first_tokens(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("1.in", "2\n3 4\n")
            archive.writestr("1.out", "7\n")
        inventory = _zip_inventory(
            ContentFile(buffer.getvalue(), name="tests.zip"),
            [{"id": 3, "type": "C", "input_file": "1.in", "output_file": "1.out"}],
        )
        self.assertEqual(inventory["records"][0]["input_full_small"], "2\n3 4\n")
        self.assertEqual(inventory["errors"], [])

    def test_zero_case_or_historical_unbound_evidence_is_incomplete(self):
        snapshot = {
            "kind": "problem",
            "statement": "Statement",
            "tests": [],
            "artifacts": {},
            "solutions": [],
            "validation": {"status": "D", "passed": True, "total_cases": 0},
            "data_config": {"checker": "standard"},
        }
        self.assertTrue(preparation_errors(snapshot))
