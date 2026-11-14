import copy
import io
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from judge.auto_review.phases import (
    build_stage_input,
)
from judge.auto_review.pdf_statement import extract_pdf_statement, review_statement_text
from judge.auto_review.review_hash import (
    problem_review_content,
    problem_review_input_hash,
)
from judge.auto_review.preparation import preparation_errors


def run_fixture(snapshot):
    return SimpleNamespace(
        problem_id=1,
        _review_context={"problem_id": 1, **snapshot},
        coordinator_stages=Mock(filter=Mock(return_value=[])),
    )


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
            self.assertNotEqual(
                problem_review_input_hash(before), problem_review_input_hash(after)
            )
            self.assertEqual(
                problem_review_content(before), problem_review_content(after)
            )

    def test_policy_instructions_and_deferred_attachments_do_not_invalidate(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after.update(
            test_data_instructions={"sha256": "new"},
            attachments=[{"filename": "data.csv"}],
        )
        self.assertEqual(
            problem_review_input_hash(before), problem_review_input_hash(after)
        )

    def test_optional_starter_templates_are_hashed_but_not_execution_inputs(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["language_templates"] = [{"language": "PY3", "source": "# starter"}]
        self.assertNotEqual(
            problem_review_content(before), problem_review_content(after)
        )
        context = build_stage_input(run_fixture(snapshot=after), "solutions_review")
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
        self.assertNotEqual(
            problem_review_content(before), problem_review_content(after)
        )
        self.assertFalse(
            any("readable problem statement" in e for e in preparation_errors(after))
        )
        for stage in (
            "problem_contract",
            "independent_analysis",
            "similarity_search",
            "statement_review",
            "solutions_review",
        ):
            self.assertIn(
                "PDF task and constraints",
                build_stage_input(run_fixture(snapshot=after), stage)["statement"],
            )
        self.assertIn("extracted text", review_statement_text(after))


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
            "judge.auto_review.pdf_statement.subprocess.run",
            side_effect=subprocess.TimeoutExpired("pdf", 10),
        ):
            result = extract_pdf_statement(self.field(b"pdf"))
        self.assertTrue(result["requires_manual_review"])
