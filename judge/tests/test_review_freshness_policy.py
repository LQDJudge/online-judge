import copy
import io
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from judge.review.coordinator import (
    build_stage_input,
    effective_readiness,
    execute_stage,
    stage_definitions,
)
from judge.review.pdf_statement import extract_pdf_statement, review_statement_text
from judge.review.snapshots import (
    execution_package_identity,
    execution_results_changed,
    preparation_errors,
    problem_review_content,
    problem_review_input_hash,
)
from judge.tests.test_coordinator_pipeline import run_fixture


@override_settings(LANGUAGE_CODE="en")
class ReviewFreshnessPolicyTests(SimpleTestCase):
    def snapshot(self):
        return {
            "kind": "problem",
            "statement": "Compute the answer",
            "config": {"time_limit": 2},
            "data_config": {"checker": "standard"},
            "tests": [],
            "artifacts": {},
            "solutions": [
                {
                    "ref": "solution:1",
                    "name": "Main",
                    "source": "source",
                    "language": "PY3",
                    "expected_result": "AC",
                    "evidence": {
                        "id": 1,
                        "status": "D",
                        "result": "AC",
                        "points": 10,
                        "time": 0.01,
                        "cases": [
                            {
                                "id": 1,
                                "case": 1,
                                "status": "AC",
                                "points": 1,
                                "total": 1,
                                "time": 0.01,
                            }
                        ],
                    },
                }
            ],
            "validation": {
                "id": 1,
                "status": "D",
                "passed": True,
                "total_cases": 1,
                "failed_count": 0,
            },
        }

    def test_solution_rename_and_record_runtime_noise_are_not_changes(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["solutions"][0]["name"] = "Renamed"
        after["solutions"][0]["evidence"].update(
            id=99, user_id=100, time=0.015, memory=222, date="today"
        )
        after["solutions"][0]["evidence"]["cases"][0].update(
            id=999, time=0.014, memory=300
        )
        after["validation"].update(id=80, user_id=100, date="tomorrow")
        self.assertEqual(
            problem_review_input_hash(before), problem_review_input_hash(after)
        )
        self.assertFalse(execution_results_changed(before, after))

    def test_meaningful_result_changes_are_evidence_not_content_changes(self):
        before = self.snapshot()
        for mutate in (
            lambda s: s["solutions"][0]["evidence"].update(result="WA"),
            lambda s: s["solutions"][0]["evidence"].update(points=5),
            lambda s: s["solutions"][0]["evidence"]["cases"][0].update(status="WA"),
            lambda s: s["validation"].update(passed=False, failed_count=1),
        ):
            after = copy.deepcopy(before)
            mutate(after)
            self.assertTrue(execution_results_changed(before, after))
            self.assertNotEqual(
                problem_review_input_hash(before), problem_review_input_hash(after)
            )
            self.assertEqual(
                problem_review_content(before), problem_review_content(after)
            )

    def test_in_progress_or_infrastructure_results_do_not_erase_reviewed_evidence(self):
        before = self.snapshot()
        for status, result in (("QU", ""), ("P", ""), ("D", "IE")):
            after = copy.deepcopy(before)
            after["solutions"][0]["evidence"].update(status=status, result=result)
            after["validation"].update(status="E")
            self.assertFalse(execution_results_changed(before, after))

    def test_policy_instructions_and_deferred_attachments_do_not_invalidate(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after.update(
            policy_version=500,
            test_data_instructions={"sha256": "new"},
            attachments=[{"filename": "data.csv"}],
        )
        self.assertEqual(
            problem_review_input_hash(before), problem_review_input_hash(after)
        )

    def test_pending_result_does_not_shift_other_solution_identities(self):
        before = self.snapshot()
        before["solutions"].append(
            {
                "source": "another",
                "language": "PY3",
                "expected_result": "AC",
                "evidence": {"status": "D", "result": "TLE"},
            }
        )
        after = copy.deepcopy(before)
        after["solutions"][1]["evidence"] = {"status": "QU"}
        self.assertFalse(execution_results_changed(before, after))

    def test_unreadable_pdf_only_finishes_manual_without_provider_calls(self):
        snapshot = self.snapshot()
        snapshot.update(
            statement="", pdf_statement={"text": "", "requires_manual_review": True}
        )
        snapshot["artifacts"]["pdf_description"] = {"sha256": "scan", "size": 12}
        run = run_fixture(snapshot=snapshot)
        with patch("judge.review.coordinator._call") as provider:
            output = execute_stage(run, "R_FINAL", {"kind": "problem"})
        provider.assert_not_called()
        self.assertTrue(output["manual_statement"])
        outputs = {name: output for name in stage_definitions("problem")}
        outputs["D"] = {"complete": True}
        self.assertEqual(
            effective_readiness(run_fixture(snapshot=snapshot, outputs=outputs)),
            "MANUAL_REVIEW",
        )

    def test_optional_starter_templates_are_hashed_but_not_execution_inputs(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["language_templates"] = [{"language": "PY3", "source": "# starter"}]
        self.assertNotEqual(
            problem_review_content(before), problem_review_content(after)
        )
        self.assertEqual(
            execution_package_identity(before), execution_package_identity(after)
        )
        context = build_stage_input(run_fixture(snapshot=after), "H2")
        self.assertEqual(context["language_templates"], after["language_templates"])

    def test_pdf_is_statement_source_not_judging_input(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["statement"] = ""
        after["artifacts"]["pdf_description"] = {
            "sha256": "pdf",
            "size": 10,
            "filename": "statement.pdf",
        }
        after["pdf_statement"] = {
            "text": "PDF task and constraints",
            "requires_manual_review": False,
        }
        self.assertEqual(
            execution_package_identity(before), execution_package_identity(after)
        )
        self.assertNotEqual(
            problem_review_content(before), problem_review_content(after)
        )
        self.assertFalse(
            any("readable problem statement" in e for e in preparation_errors(after))
        )
        for stage in ("E", "FG", "S", "H1", "H2_BLIND"):
            self.assertIn(
                "PDF task and constraints",
                build_stage_input(run_fixture(snapshot=after), stage)["statement"],
            )
        self.assertIn("extracted text", review_statement_text(after))

    def test_contest_ai_does_not_receive_team_exposure_or_absolute_dates(self):
        snapshot = {
            "kind": "contest",
            "config": {
                "name": "Secret title",
                "start_time": "2026-10-01T09:00:00",
                "end_time": "2026-10-01T11:00:00",
            },
            "items": [],
            "children": [],
            "exposure": {"material_concern": True},
        }
        run = run_fixture(snapshot=snapshot)
        for stage in ("E", "H1", "H2"):
            context = build_stage_input(run, stage)
            data = context.get("dossier", context)
            self.assertNotIn("exposure", data)
            self.assertNotIn("start_time", data["config"])
            self.assertEqual(data["config"]["duration_seconds"], 7200)

    def test_exposure_does_not_force_manual_quality_result(self):
        outputs = {name: {} for name in stage_definitions("contest")}
        outputs.update(
            D={"complete": True},
            R={"readiness": "READY"},
            E={"children": [], "exposure": {"material_concern": True}},
        )
        run = run_fixture(
            snapshot={"kind": "contest"},
            outputs=outputs,
        )
        self.assertEqual(effective_readiness(run), "READY")


class PdfStatementExtractionTests(SimpleTestCase):
    def pdf(self, text=True, encrypted=False):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 200 Td (Compute N plus one.) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
        if encrypted:
            writer.encrypt("password")
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def field(self, data):
        return SimpleNamespace(
            name="statement.pdf",
            storage=SimpleNamespace(open=lambda *args: io.BytesIO(data)),
        )

    def test_real_text_pdf_extracts_in_resource_limited_process(self):
        result = extract_pdf_statement(self.field(self.pdf()))
        self.assertIn("Compute N plus one.", result["text"])
        self.assertFalse(result["requires_manual_review"])

    def test_blank_encrypted_and_malformed_pdfs_are_manual_not_false_passes(self):
        for data in (self.pdf(text=False), self.pdf(encrypted=True), b"not a PDF"):
            with self.subTest(data=data[:10]):
                self.assertTrue(
                    extract_pdf_statement(self.field(data))["requires_manual_review"]
                )

    def test_timeout_is_bounded_manual_limitation(self):
        with patch(
            "judge.review.pdf_statement.subprocess.run",
            side_effect=subprocess.TimeoutExpired("pdf", 10),
        ):
            result = extract_pdf_statement(self.field(b"pdf"))
        self.assertTrue(result["requires_manual_review"])
