import copy
import json
from pathlib import Path

from django.test import SimpleTestCase

from judge.review.benchmark import score_benchmark


class ReviewBenchmarkTests(SimpleTestCase):
    def setUp(self):
        self.suite = {
            "version": "offline-scorer-test",
            "cases": [
                {
                    "id": "clean",
                    "kind": "clean",
                    "allowed_readiness": ["READY"],
                    "defects": [],
                },
                {
                    "id": "bad",
                    "kind": "defect",
                    "allowed_readiness": ["NEEDS_AUTHOR_FIX"],
                    "defects": [{"id": "wrong_sample", "stages": ["H1"]}],
                },
                {
                    "id": "outage",
                    "kind": "infrastructure",
                    "allowed_readiness": ["INCOMPLETE"],
                    "defects": [],
                },
            ],
        }
        self.observations = [
            {
                "case_id": "clean",
                "run_id": 1,
                "input_hash": "a",
                "readiness": "READY",
                "infrastructure_failed": False,
                "adjudicated_by": "offline unit-test fixture",
            },
            {
                "case_id": "bad",
                "run_id": 2,
                "input_hash": "b",
                "readiness": "NEEDS_AUTHOR_FIX",
                "infrastructure_failed": False,
                "adjudicated_by": "offline unit-test fixture",
                "findings": [
                    {
                        "ref": "H1:0",
                        "reasoning": "Sample contradicts definition",
                        "evidence_refs": ["statement"],
                    }
                ],
                "adjudications": [
                    {
                        "defect_id": "wrong_sample",
                        "detected": True,
                        "finding_ref": "H1:0",
                    }
                ],
            },
            {
                "case_id": "outage",
                "run_id": 3,
                "input_hash": "c",
                "readiness": "INCOMPLETE",
                "infrastructure_failed": True,
            },
        ]

    def test_complete_adjudicated_results_pass_scoring_not_model_certification(self):
        report = score_benchmark(self.suite, self.observations)
        self.assertTrue(report["passed"])
        self.assertEqual(report["totals"]["confirmed_detections"], 1)
        self.assertEqual(report["defect_detection_rate"], 1)

    def test_missing_runs_and_missing_adjudication_do_not_pass(self):
        self.assertFalse(score_benchmark(self.suite, [])["passed"])
        self.observations[1]["adjudications"] = []
        report = score_benchmark(self.suite, self.observations)
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["unadjudicated"], 1)

    def test_false_pass_is_recorded_even_when_defect_was_detected(self):
        self.observations[1]["readiness"] = "READY"
        report = score_benchmark(self.suite, self.observations)
        self.assertFalse(report["passed"])
        self.assertEqual(report["totals"]["false_passes"], 1)

    def test_infrastructure_failures_are_not_successful_negative_reviews(self):
        self.observations[1]["infrastructure_failed"] = True
        report = score_benchmark(self.suite, self.observations)
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["unavailable"], 1)

    def test_false_blocks_and_missed_defects_are_separate_metrics(self):
        self.observations[0]["readiness"] = "NEEDS_AUTHOR_FIX"
        self.observations[1]["adjudications"] = [
            {"defect_id": "wrong_sample", "detected": False}
        ]
        report = score_benchmark(self.suite, self.observations)
        self.assertEqual(report["totals"]["false_blocks"], 1)
        self.assertEqual(report["totals"]["missed_defects"], 1)
        self.assertFalse(report["passed"])

    def test_annotations_require_existing_evidence_backed_findings(self):
        for value in ("H1:missing", "H3:0"):
            with self.subTest(ref=value):
                observations = copy.deepcopy(self.observations)
                observations[1]["adjudications"][0]["finding_ref"] = value
                with self.assertRaises(ValueError):
                    score_benchmark(self.suite, observations)

    def test_duplicate_or_unknown_runs_are_rejected(self):
        with self.assertRaises(ValueError):
            score_benchmark(self.suite, self.observations + self.observations[:1])
        self.observations[0]["case_id"] = "unknown"
        with self.assertRaises(ValueError):
            score_benchmark(self.suite, self.observations)

    def test_readiness_alone_cannot_claim_model_defect_detection(self):
        del self.observations[1]["adjudicated_by"]
        self.assertFalse(score_benchmark(self.suite, self.observations)["complete"])

    def test_ground_truth_suite_covers_agreed_defect_families(self):
        path = Path(__file__).resolve().parents[1] / "review" / "benchmark_cases.json"
        suite = json.loads(path.read_text())
        self.assertEqual(len(suite["cases"]), 10)
        self.assertFalse(score_benchmark(suite, [])["passed"])
