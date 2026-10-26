"""Live publication explanations must agree with eligibility, not rewrite reviews."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from judge.models import Contest, Problem
from judge.review.workflow import publication_blockers, publication_eligibility


@override_settings(LANGUAGE_CODE="en")
class PublicationBlockerTests(SimpleTestCase):
    def setUp(self):
        self.problem = Problem(id=1, code="example")
        self.stages = [SimpleNamespace(stage_id="D", status="D", DONE="D")]
        self.run = SimpleNamespace(
            workflow_version=2,
            status="D",
            DONE="D",
            readiness="READY",
            problem_id=1,
            content_hash="a",
            input_hash="input",
            snapshot={},
            coordinator_stages=SimpleNamespace(all=lambda: self.stages),
        )
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (
            ("reviewed_content_hash", "a"),
            ("current_content_hash", "a"),
            ("publication_exposure_concern", False),
            ("stage_definitions", ["D"]),
            ("build_problem_snapshot", {"input_hash": "input"}),
            ("preparation_errors", []),
        ):
            self.patches.enter_context(
                patch("judge.review.workflow." + name, return_value=value)
            )

    def test_passing_review_has_no_blockers(self):
        self.assertEqual(publication_blockers(self.run, self.problem), [])
        self.assertTrue(publication_eligibility(self.run, self.problem))

    def test_presentation_snapshot_reuse_does_not_cache_publication_authority(self):
        snapshot = {"content_hash": "a", "input_hash": "input"}
        with patch(
            "judge.review.workflow.current_content_hash", return_value="changed"
        ) as current_hash, patch(
            "judge.review.workflow.build_problem_snapshot"
        ) as capture:
            self.assertEqual(
                publication_blockers(self.run, self.problem, current_snapshot=snapshot),
                [],
            )
            current_hash.assert_not_called()
            capture.assert_not_called()
            # The publication API never supplies a presentation snapshot: it
            # must discover a later edit instead of trusting the displayed pass.
            self.assertFalse(publication_eligibility(self.run, self.problem))
            current_hash.assert_called_once_with(self.problem)

    def test_stale_content_explains_required_rerun(self):
        with patch(
            "judge.review.workflow.current_content_hash", return_value="changed"
        ):
            self.assertIn(
                "content has changed", publication_blockers(self.run, self.problem)[0]
            )

    def test_missing_or_unfinished_stages_are_explained(self):
        self.stages.clear()
        self.assertIn(
            "missing or unfinished", publication_blockers(self.run, self.problem)[0]
        )

    def test_execution_evidence_mismatch_is_explained(self):
        self.run.input_hash = "old-input"
        self.assertIn(
            "Run Validation and Solution Codes",
            publication_blockers(self.run, self.problem)[0],
        )

    def test_missing_review_and_unready_result_are_not_publishable(self):
        self.assertIn("Complete a review", publication_blockers(None, self.problem)[0])
        self.run.readiness = "MANUAL_REVIEW"
        self.assertFalse(publication_eligibility(self.run, self.problem))

    def test_exposure_blocks_contest_only_without_changing_saved_verdicts(self):
        contest = Contest(id=2)
        dependencies = Mock()
        dependencies.select_related.return_value.prefetch_related.return_value = [
            SimpleNamespace(problem_id=1, problem_run=self.run, content_hash="a")
        ]
        parent = SimpleNamespace(
            workflow_version=2,
            status="D",
            DONE="D",
            readiness="READY",
            contest_id=2,
            child_dependencies=dependencies,
            _review_dependency_rows=[],
            _latest_completed_problem_reviews={},
            coordinator_stages=SimpleNamespace(all=lambda: self.stages),
        )
        for parent_exposed in (False, True):
            with self.subTest(parent_exposed=parent_exposed), patch.object(
                Problem, "get_cached_instances", return_value=[self.problem]
            ), patch(
                "judge.review.workflow.publication_exposure_concern",
                side_effect=lambda obj: isinstance(obj, Contest) and parent_exposed,
            ):
                reasons = publication_blockers(parent, contest)
                self.assertEqual(publication_blockers(self.run, self.problem), [])
            self.assertEqual(len(reasons), int(parent_exposed))
            if parent_exposed:
                self.assertIn("Possible pre-release access", reasons[0])
                self.assertIn("not proof", reasons[0])
            self.assertEqual(parent.readiness, "READY")
            self.assertEqual(self.run.readiness, "READY")
