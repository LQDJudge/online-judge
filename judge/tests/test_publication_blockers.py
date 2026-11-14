"""Live publication explanations must agree with eligibility, not rewrite reviews."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from judge.models import Contest, Problem
from judge.auto_review.workflow import publication_blockers, publication_eligibility


@override_settings(LANGUAGE_CODE="en")
class PublicationBlockerTests(SimpleTestCase):
    def setUp(self):
        self.problem = Problem(id=1, code="example")
        self.stages = [
            SimpleNamespace(
                stage_id="statement_review", status="D", DONE="D", output_json={}
            )
        ]
        self.run = SimpleNamespace(
            status="D",
            DONE="D",
            readiness="READY",
            problem_id=1,
            content_hash="a",
            input_hash="a",
            coordinator_stages=SimpleNamespace(all=lambda: self.stages),
        )
        self.patches = ExitStack()
        self.patches.enter_context(
            patch(
                "judge.auto_review.workflow._runs",
                return_value=Mock(
                    filter=Mock(return_value=Mock(exists=Mock(return_value=False)))
                ),
            )
        )
        self.addCleanup(self.patches.close)
        for name, value in (
            ("reviewed_content_hash", "a"),
            ("current_content_hash", "a"),
            ("publication_exposure_concern", False),
            ("definitions_for_run", {"statement_review": {}}),
            ("preparation_errors", []),
        ):
            self.patches.enter_context(
                patch("judge.auto_review.workflow." + name, return_value=value)
            )

    def test_passing_review_has_no_blockers(self):
        self.assertEqual(publication_blockers(self.run, self.problem), [])
        self.assertTrue(publication_eligibility(self.run, self.problem))

    def test_presentation_snapshot_reuse_does_not_cache_publication_authority(self):
        snapshot = {"input_hash": "a"}
        with patch(
            "judge.auto_review.workflow.current_content_hash", return_value="changed"
        ) as current_hash:
            self.assertEqual(
                publication_blockers(self.run, self.problem, current_context=snapshot),
                [],
            )
            current_hash.assert_not_called()
            # The publication API never supplies a presentation snapshot: it
            # must discover a later edit instead of trusting the displayed pass.
            self.assertFalse(publication_eligibility(self.run, self.problem))
            current_hash.assert_called_once_with(self.problem)

    def test_stale_content_explains_required_rerun(self):
        with patch(
            "judge.auto_review.workflow.current_content_hash", return_value="changed"
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
            "content has changed",
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
            status="D",
            DONE="D",
            readiness="READY",
            contest_id=2,
            input_hash="a",
            child_dependencies=dependencies,
            _review_dependency_rows=[],
            _latest_completed_problem_reviews={},
            coordinator_stages=SimpleNamespace(all=lambda: self.stages),
        )
        for parent_exposed in (False, True):
            with self.subTest(parent_exposed=parent_exposed), patch.object(
                Problem, "get_cached_instances", return_value=[self.problem]
            ), patch(
                "judge.auto_review.workflow.contest_child_readiness", return_value=None
            ), patch(
                "judge.auto_review.workflow.publication_exposure_concern",
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
