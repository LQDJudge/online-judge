"""Regression checks for review admission and bounded test inventory."""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.test import RequestFactory, SimpleTestCase, override_settings

from ai_features.problem_tagger import ProblemTagger
from judge.auto_review.problem_context import _zip_inventory
from judge.auto_review.context_utils import canonical_json
from judge.auto_review.preparation import preparation_errors
from judge.auto_review.phases import _retrieve
from judge.views.problem_data import ProblemValidatorSaveView


@override_settings(LANGUAGE_CODE="en")
class ReviewInputRegressionTests(SimpleTestCase):
    def test_problem_tagger_includes_shared_difficulty_rules(self):
        tagger = ProblemTagger.__new__(ProblemTagger)
        tagger.llm_service = Mock()
        tagger.llm_service.call_llm_with_files.return_value = (
            '{"is_valid": true, "points": 1000, "tags": ["dp"]}'
        )
        result = tagger.analyze_and_tag_problem("Statement", ["dp"])
        self.assertTrue(result["is_valid"])
        self.assertIn(
            "DIFFICULTY",
            tagger.llm_service.call_llm_with_files.call_args.args[2].upper(),
        )

    def test_validator_save_rejects_invalid_input_cleanly(self):
        view = ProblemValidatorSaveView()
        view.get_object = Mock(return_value=object())
        for body in ("{", '{"language": "unknown"}', '{"code": 12}'):
            with self.subTest(body=body):
                request = RequestFactory().post(
                    "/validator", data=body, content_type="application/json"
                )
                response = view.post(request)
                self.assertEqual(response.status_code, 400)

    @patch("judge.auto_review.phases.Problem.objects.filter")
    @patch("judge.auto_review.phases.semantic_search._semantic_vector_search")
    @patch("judge.auto_review.phases._frozen_query_embedding")
    @patch(
        "judge.auto_review.phases.semantic_search.is_semantic_search_enabled",
        return_value=True,
    )
    def test_semantic_retrieval_returns_eligible_candidates_and_timestamp(
        self, enabled, embed, search, query
    ):
        search.return_value = [(1, 0.9), (2, 0.8)]
        query.return_value = [
            SimpleNamespace(
                pk=1,
                code="public",
                description="Statement",
                time_limit=1,
                memory_limit=65536,
            )
        ]
        result = _retrieve({"problem_id": 3, "statement": "Query"})
        self.assertEqual([item["problem_id"] for item in result["candidates"]], [1])
        self.assertTrue(result["retrieved_at"])
        query.assert_called_once_with(
            pk__in=[1, 2], is_public=True, is_organization_private=False
        )

    def example(self, checker="standard"):
        return {
            "kind": "problem",
            "statement": "A statement",
            "data_config": {"output_only": True, "checker": checker},
            "artifacts": {"zipfile": {"filename": "tests.zip"}},
            "tests": [{"type": "C"}],
            "output_examples": [
                {
                    "artifact": {"revision": "present"},
                    "archive_summary": {"valid": True},
                    "evidence": {
                        "status": "D",
                        "result": "AC",
                        "execution_hash": "same",
                        "current_execution_hash": "same",
                        "case_points": 1,
                        "case_total": 1,
                        "cases": [{"status": "AC", "points": 1, "total": 1}],
                    },
                }
            ],
        }

    def test_exact_output_requires_a_valid_fully_accepted_example(self):
        data = self.example()
        self.assertEqual(preparation_errors(data), [])
        evidence = data["output_examples"][0]["evidence"]
        evidence.update(
            result="WA",
            case_points=0,
            cases=[{"status": "WA", "points": 0, "total": 1}],
        )
        self.assertTrue(preparation_errors(data))
        evidence.update(
            result="AC",
            case_points=1,
            cases=[{"status": "AC", "points": 1, "total": 1}],
        )
        data["output_examples"][0]["archive_summary"] = {"available": False}
        self.assertTrue(preparation_errors(data))

    def test_old_output_example_requires_a_new_execution(self):
        data = self.example()
        evidence = data["output_examples"][0]["evidence"]
        evidence["execution_hash"] = ""
        self.assertTrue(any("output example" in e for e in preparation_errors(data)))
        evidence["execution_hash"] = evidence["current_execution_hash"]
        self.assertEqual(preparation_errors(data), [])

    def test_metric_output_accepts_a_valid_nonperfect_result(self):
        data = self.example("csv_accuracy")
        data["output_examples"][0]["evidence"].update(
            result="WA",
            case_points=0,
            cases=[{"status": "WA", "points": 0, "total": 1}],
        )
        self.assertEqual(preparation_errors(data), [])

    def test_large_number_of_small_previews_has_bounded_total(self):
        raw = io.BytesIO()
        tests = []
        with zipfile.ZipFile(raw, "w") as archive:
            for index in range(50):
                archive.writestr(f"{index}.in", "x" * 4000)
                archive.writestr(f"{index}.out", "x" * 4000)
                tests.append(
                    {
                        "id": index,
                        "type": "C",
                        "input_file": f"{index}.in",
                        "output_file": f"{index}.out",
                    }
                )
        result = _zip_inventory(ContentFile(raw.getvalue(), name="tests.zip"), tests)
        self.assertEqual(len(result["records"]), 50)
        self.assertLess(len(canonical_json(result)), 100000)
        self.assertTrue(any("omitted" in item for item in result["limitations"]))
