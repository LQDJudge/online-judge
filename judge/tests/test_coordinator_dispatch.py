from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import ReviewStage, ContestReviewRun, ContestReviewDependency
from judge.review import dispatch, workflow
from judge.tests import test_coordinator_workflow as workflow_tests


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
class CoordinatorDispatchTests(TestCase):
    setUpTestData = classmethod(
        workflow_tests.CoordinatorWorkflowTest.setUpTestData.__func__
    )
    make_run = workflow_tests.CoordinatorWorkflowTest.make_run

    def setUp(self):
        self.snapshot = {
            "kind": "problem",
            "content_hash": "a" * 64,
            "input_hash": "b" * 64,
        }

    def test_parent_overlay_recovers_when_child_returns_to_exact_reviewed_content(self):
        child = self.make_run()
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            snapshot={},
            content_hash="original",
            input_hash="original",
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash=child.content_hash,
        )
        with patch(
            "judge.review.workflow.current_content_hash",
            side_effect=lambda obj, **kwargs: (
                "changed" if obj is self.problem else "changed-parent"
            ),
        ):
            workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertTrue(parent.current_stale)
        with patch(
            "judge.review.workflow.current_content_hash",
            side_effect=lambda obj, **kwargs: (
                child.content_hash if obj is self.problem else "original"
            ),
        ):
            workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertFalse(parent.current_stale)

    def test_initial_broker_outage_persists_run_and_cancellation_is_free(self):
        app = MagicMock()
        app.send_task.side_effect = ConnectionError("test broker unavailable")
        app.tasks.get.return_value = None
        with patch("judge.review.dispatch.current_app", app), patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ), patch("judge.review.workflow.preparation_errors", return_value=[]):
            with self.captureOnCommitCallbacks(execute=True):
                run = workflow.start_problem_review(self.problem, self.author.profile)
        stage = run.coordinator_stages.get(stage_id="D")
        self.assertEqual(stage.error_category, "broker_delivery")
        self.assertEqual((stage.attempts, stage.delivery_attempts), (0, 1))
        workflow.cancel_review(run, self.author.profile)
        run.refresh_from_db()
        self.assertFalse(run.credit_consumed)

    def test_delivery_retries_are_bounded_and_never_touch_completed_outputs(self):
        run = self.make_run()
        done = ReviewStage.objects.create(
            problem_run=run,
            stage_id="E",
            status="D",
            attempts=1,
            output_json={"saved": True},
        )
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="FG", input_json={"frozen": True}, attempts=1
        )
        with patch(
            "judge.review.dispatch.current_app.send_task",
            side_effect=ConnectionError("broker"),
        ):
            dispatch.dispatch_stage("problem", run.pk, "FG")
            dispatch.dispatch_stage("problem", run.pk, "FG")
            stage.refresh_from_db()
            self.assertEqual(stage.delivery_attempts, 1)
            for _ in range(2):
                ReviewStage.objects.filter(pk=stage.pk).update(
                    retry_at=timezone.now() - timedelta(seconds=1)
                )
                dispatch.dispatch_stage("problem", run.pk, "FG")
        stage.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(
            (stage.status, stage.delivery_attempts, stage.attempts), ("E", 3, 1)
        )
        self.assertEqual(done.output_json, {"saved": True})
        self.assertEqual(stage.input_json, {"frozen": True})

    def test_successful_redelivery_restores_running_without_provider_reset(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        stage = ReviewStage.objects.create(
            problem_run=run,
            stage_id="E",
            attempts=2,
            delivery_attempts=2,
            error_category="broker_delivery",
            retry_at=timezone.now() - timedelta(seconds=1),
        )
        with patch("judge.review.dispatch.current_app.send_task") as send:
            dispatch.dispatch_stage("problem", run.pk, "E")
        run.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(
            (run.status, stage.attempts, stage.error_category), ("R", 2, "")
        )
        send.assert_called_once()

    def test_inline_scheduler_cannot_recursively_retry_delayed_wait(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="D")
        app = MagicMock()
        app.send_task.side_effect = ConnectionError("broker")
        scheduler = app.tasks.get.return_value
        scheduler.run.side_effect = lambda kind, pk: dispatch.dispatch_advance(
            kind, pk, countdown=15
        )
        with patch("judge.review.dispatch.current_app", app):
            dispatch.dispatch_advance("problem", run.pk)
        scheduler.run.assert_called_once()
        self.assertEqual(app.send_task.call_count, 2)
        stage.refresh_from_db()
        self.assertEqual(stage.delivery_attempts, 1)

    def test_cancelled_run_is_not_dispatched(self):
        run = self.make_run(status="C")
        ReviewStage.objects.create(problem_run=run, stage_id="D")
        with patch("judge.review.dispatch.current_app.send_task") as send:
            dispatch.dispatch_stage("problem", run.pk, "D")
        send.assert_not_called()
