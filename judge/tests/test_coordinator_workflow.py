from datetime import timedelta
import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.translation import override

from bs4 import BeautifulSoup

from judge.models import (
    Contest,
    Comment,
    ContestProblem,
    ContestReviewDependency,
    ContestReviewRun,
    ContestPublicRequest,
    Language,
    Problem,
    ProblemData,
    ProblemGroup,
    LanguageTemplate,
    ProblemReviewRun,
    Profile,
    PublicRequest,
    Quiz,
    ReviewStage,
    ReviewStageAttempt,
    ReviewEvidenceJob,
    Submission,
)
from judge.review import workflow
from judge.review.coordinator import (
    CoordinatorInfrastructureError,
    CoordinatorOutputError,
    stage_definitions as coordinator_stage_definitions,
    _call,
    _claim_is_active,
    validate_output,
)
from judge.review.snapshots import (
    SnapshotError,
    build_contest_snapshot,
    build_problem_manifest,
    problem_review_input_hash,
    reviewed_content_hash,
)
from judge.tasks.coordinator import (
    advance_review,
    cleanup_review_diagnostics,
    emit_completion,
    run_review_stage,
    failure_category,
)
from judge.views.coordinator_review import coordinator_context
from judge.models.notification import Notification, NotificationCategory


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True, LANGUAGE_CODE="en")
class CoordinatorWorkflowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        group, _ = ProblemGroup.objects.get_or_create(
            name="coordinator", defaults={"full_name": "Coordinator tests"}
        )
        cls.author = User.objects.create_user("review_author", password="password")
        cls.other = User.objects.create_user("review_other", password="password")
        cls.admin = User.objects.create_superuser(
            "review_admin", "review@example.invalid", "password"
        )
        for user in (cls.author, cls.other, cls.admin):
            Profile.objects.get_or_create(user=user, defaults={"language": language})
            user.user_permissions.add(Permission.objects.get(codename="add_problem"))
        cls.problem = Problem.objects.create(
            code="review_fixture",
            name="Review fixture",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
            description="Test",
        )
        cls.problem.authors.add(cls.author.profile, cls.other.profile)
        cls.contest = Contest.objects.create(
            key="review_fixture",
            name="Review fixture",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
        )
        cls.contest.authors.add(cls.author.profile)
        ContestProblem.objects.create(
            contest=cls.contest, problem=cls.problem, points=10, order=0
        )

    def setUp(self):
        self.snapshot = {
            "kind": "problem",
            "content_hash": "a" * 64,
            "input_hash": "b" * 64,
        }
        self.enqueue = patch("judge.review.workflow.enqueue").start()
        patch(
            "judge.tasks.coordinator.stage_definitions",
            return_value={"E": {"dependencies": []}},
        ).start()
        self.addCleanup(patch.stopall)

    def test_contest_usage_includes_only_newly_owned_child_attempts(self):
        child = self.make_run()
        owned_stage = ReviewStage.objects.create(problem_run=child, stage_id="E")
        ReviewStageAttempt.objects.create(
            stage=owned_stage, number=1, usage_json={"estimated_tokens": 77}
        )
        reused_problem = Problem.objects.create(
            code="review_reused",
            name="Reused report",
            group_id=self.problem.group_id,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        reused = ProblemReviewRun.objects.create(problem=reused_problem)
        reused_stage = ReviewStage.objects.create(problem_run=reused, stage_id="E")
        ReviewStageAttempt.objects.create(
            stage=reused_stage, number=1, usage_json={"estimated_tokens": 999}
        )
        contest_run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            content_hash="a" * 64,
            snapshot={"kind": "contest"},
        )
        parent_stage = ReviewStage.objects.create(contest_run=contest_run, stage_id="R")
        ReviewStageAttempt.objects.create(
            stage=parent_stage, number=1, usage_json={"estimated_tokens": 23}
        )
        ContestReviewDependency.objects.create(
            contest_run=contest_run, problem=self.problem, problem_run=child, owned=True
        )
        ContestReviewDependency.objects.create(
            contest_run=contest_run, problem=reused_problem, problem_run=reused
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            context = coordinator_context(self.contest, contest_run, self.author)
        self.assertEqual(context["coordinator_usage_tokens"], 100)

    def make_run(self, **kwargs):
        values = {
            "problem": self.problem,
            "triggered_by": self.author.profile,
            "workflow_version": 2,
            "snapshot": self.snapshot,
            "content_hash": self.snapshot["content_hash"],
            "input_hash": self.snapshot["input_hash"],
        }
        values.update(kwargs)
        return ProblemReviewRun.objects.create(**values)

    @override_settings(RATELIMIT_ENABLE=False)
    def test_post_and_reply_return_fresh_discussion_count(self):
        run = self.make_run()
        content_type = ContentType.objects.get_for_model(run)
        self.client.force_login(self.admin)
        data = {
            "content_type_id": content_type.pk,
            "object_id": run.pk,
            "body": "Review discussion",
            "parent": "",
        }
        response = self.client.post("/comments/post/", data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Comment-Count"], "1")
        parent = Comment.objects.get(content_type=content_type, object_id=run.pk)
        response = self.client.post(
            "/comments/post/", {**data, "parent": parent.pk, "body": "Review reply"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Comment-Count"], "2")
        self.assertTrue(Comment.objects.filter(parent=parent).exists())
        response = self.client.post("/comments/post/", {**data, "body": ""})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("X-Comment-Count", response)

    def start(self, actor=None, confirm=False):
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ), patch("judge.review.workflow.preparation_errors", return_value=[]):
            return workflow.start_problem_review(
                self.problem, (actor or self.author).profile, confirm_unchanged=confirm
            )

    def drive_best_effort(self, run, failed_stages=()):
        """Real scheduler/context/probe routing; only provider responses are mocked."""
        calls = []

        def analysis(current_run, stage_id, context):
            calls.append((stage_id, context))
            if stage_id in failed_stages:
                raise CoordinatorInfrastructureError("test provider unavailable")
            result = {
                "status": "no_issue",
                "summary": "Assessment " + stage_id,
                "findings": [],
                "uncertainties": [],
                "probes": [],
            }
            if stage_id in ("R", "R_FINAL"):
                result.update(readiness="READY", follow_up_requests=[])
            return result

        def retrieve(context):
            if "S" in failed_stages:
                raise CoordinatorInfrastructureError("test search unavailable")
            return {"candidates": [], "scope": "test retrieval"}

        with patch(
            "judge.tasks.coordinator.stage_definitions",
            side_effect=coordinator_stage_definitions,
        ), patch("judge.review.coordinator._call", side_effect=analysis), patch(
            "judge.review.coordinator._retrieve", side_effect=retrieve
        ), patch(
            "judge.review.coordinator.preparation_errors", return_value=[]
        ):
            for _ in range(50):
                scheduled = []
                with patch(
                    "judge.tasks.coordinator._send_stage",
                    side_effect=lambda kind, run_id, stage_id, countdown=0: scheduled.append(
                        stage_id
                    ),
                ):
                    advance_review("problem", run.pk)
                if not scheduled:
                    break
                for stage_id in scheduled:
                    run.coordinator_stages.filter(stage_id=stage_id).update(
                        retry_at=None
                    )
                    run_review_stage("problem", run.pk, stage_id)
            else:
                self.fail("Best-effort scheduler did not reach quiescence")
        run.refresh_from_db()
        return calls

    def test_full_best_effort_run_finishes_partial_feedback_and_selective_recovery(
        self,
    ):
        run = self.make_run(
            snapshot={
                **self.snapshot,
                "statement": "Sum an array.",
                "data_config": {"checker": "standard"},
            }
        )
        calls = self.drive_best_effort(run, {"S"})
        self.assertEqual(run.status, "B")
        self.assertEqual(run.readiness, "INCOMPLETE")
        self.assertFalse(run.credit_consumed)
        self.assertEqual(run.summary_report, "Assessment R")
        stages = {stage.stage_id: stage for stage in run.coordinator_stages.all()}
        self.assertEqual(stages["S"].attempts, 3)
        self.assertEqual(stages["H7"].output_json["availability"], "unavailable")
        self.assertNotIn("H7", [name for name, _ in calls])
        self.assertEqual(stages["R"].output_json["readiness"], "INCOMPLETE")
        independent_attempts = {
            name: stages[name].attempts
            for name in ("E", "FG", "H1", "H2", "H3", "H4", "H5", "H6")
        }
        workflow.retry_stage(run, "S", self.author.profile)
        calls = self.drive_best_effort(run)
        self.assertEqual(run.status, "D")
        self.assertEqual(run.readiness, "READY")
        self.assertTrue(run.credit_consumed)
        for name, count in independent_attempts.items():
            self.assertEqual(run.coordinator_stages.get(stage_id=name).attempts, count)
        self.assertEqual([name for name, _ in calls], ["H7", "R"])
        self.assertEqual(
            run.coordinator_stages.get(stage_id="S").attempt_records.count(), 4
        )

    def test_failed_statement_reviewer_does_not_stop_synthesis_or_other_reviewers(self):
        run = self.make_run(snapshot={**self.snapshot, "statement": "Sum an array."})
        calls = self.drive_best_effort(run, {"H1"})
        self.assertEqual(run.status, "B")
        self.assertEqual(run.readiness, "INCOMPLETE")
        self.assertTrue(run.summary_report)
        self.assertFalse(run.credit_consumed)
        for name in ("H2", "H3", "H4", "H5", "H6", "H7", "R", "R_FINAL"):
            self.assertEqual(run.coordinator_stages.get(stage_id=name).status, "D")
        synthesis = next(context for name, context in calls if name == "R")
        self.assertIn(
            "H1", {item["stage_id"] for item in synthesis["missing_evidence"]}
        )
        self.assertNotIn("H1", synthesis["stages"]["Q"]["stages"])

    def test_failed_contract_keeps_raw_statement_and_no_fabricated_extraction(self):
        run = self.make_run(
            snapshot={**self.snapshot, "statement": "Raw frozen statement"}
        )
        calls = self.drive_best_effort(run, {"E"})
        self.assertEqual(run.status, "B")
        blind = next(context for name, context in calls if name == "FG")
        self.assertEqual(blind["statement"], "Raw frozen statement")
        self.assertEqual(blind["contract"], {})
        self.assertIn("E", {item["stage_id"] for item in blind["missing_evidence"]})
        self.assertFalse(
            run.coordinator_stages.get(stage_id="P_EXTRACT").output_json["executed"]
        )
        self.assertTrue(run.summary_report)
        self.assertFalse(run.credit_consumed)

    def test_infrastructure_failure_classification(self):
        self.assertEqual(
            failure_category("S", CoordinatorInfrastructureError("failed")),
            "semantic_retrieval",
        )
        self.assertEqual(
            failure_category("P_VALIDATOR", CoordinatorInfrastructureError("failed")),
            "judge_execution",
        )
        self.assertEqual(
            failure_category("H1", CoordinatorInfrastructureError("failed")), "provider"
        )
        self.assertEqual(failure_category("H1", OSError("failed")), "artifact_access")

    def test_contest_uses_blocked_child_context_and_refreshes_after_recovery(self):
        child = self.make_run(status="B", readiness="INCOMPLETE")
        child_stage = ReviewStage.objects.create(
            problem_run=child,
            stage_id="S",
            status="E",
            error_category="semantic_retrieval",
        )
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            snapshot={
                "kind": "contest",
                "children": [
                    {"review_run_id": child.pk, "content_hash": child.content_hash}
                ],
            },
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash=child.content_hash,
        )

        def drain():
            with patch(
                "judge.tasks.coordinator.stage_definitions",
                side_effect=coordinator_stage_definitions,
            ), patch(
                "judge.review.coordinator._call",
                return_value={
                    "summary": "Partial contest feedback",
                    "status": "no_issue",
                    "findings": [],
                    "uncertainties": [],
                    "readiness": "READY",
                },
            ), patch(
                "judge.review.coordinator.preparation_errors", return_value=[]
            ):
                for _ in range(12):
                    scheduled = []
                    with patch(
                        "judge.tasks.coordinator._send_stage",
                        side_effect=lambda kind, run_id, stage_id, countdown=0: scheduled.append(
                            stage_id
                        ),
                    ):
                        advance_review("contest", parent.pk)
                    if not scheduled:
                        break
                    for stage_id in scheduled:
                        run_review_stage("contest", parent.pk, stage_id)
                else:
                    self.fail("Contest scheduler did not quiesce")
            parent.refresh_from_db()

        drain()
        self.assertEqual(parent.status, "B")
        self.assertEqual(parent.readiness, "INCOMPLETE")
        self.assertFalse(parent.credit_settled)
        self.assertEqual(parent.summary_report, "Partial contest feedback")
        dossier = parent.coordinator_stages.get(stage_id="E")
        self.assertEqual(dossier.input_json["children"][0]["readiness"], "INCOMPLETE")
        self.assertTrue(dossier.input_json["missing_evidence"])
        original_d_attempts = parent.coordinator_stages.get(stage_id="D").attempts
        child_stage.status = "D"
        child_stage.error_category = ""
        child_stage.output_json = {"candidates": []}
        child_stage.save()
        child.status = "D"
        child.readiness = "READY"
        child.save()
        drain()
        self.assertEqual(parent.status, "D")
        self.assertEqual(parent.readiness, "READY")
        self.assertTrue(parent.credit_consumed)
        self.assertEqual(
            parent.coordinator_stages.get(stage_id="D").attempts, original_d_attempts
        )
        self.assertEqual(parent.coordinator_stages.get(stage_id="E").attempts, 2)

    def test_cancelling_contest_during_child_recovery_is_free_in_preview_and_settlement(
        self,
    ):
        child = self.make_run(status="R", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=child,
            stage_id="S",
            status="P",
            error_category="semantic_retrieval",
        )
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="R",
            snapshot={"kind": "contest"},
        )
        ContestReviewDependency.objects.create(
            contest_run=parent, problem=self.problem, problem_run=child
        )
        context = coordinator_context(self.contest, parent, self.author)
        self.assertEqual(context["coordinator_cancel_targets"][0]["credit"], 0)
        workflow.cancel_review(parent, self.author.profile)
        parent.refresh_from_db()
        self.assertFalse(parent.credit_consumed)

    def test_recovery_retains_independently_failed_consumer_allowance(self):
        run = self.make_run(status="B")
        ReviewStage.objects.create(
            problem_run=run, stage_id="FG", status="E", attempts=3
        )
        consumer = ReviewStage.objects.create(
            problem_run=run,
            stage_id="H1",
            status="E",
            attempts=4,
            manual_retries=1,
            input_json={"old": "context"},
            request_json={"old": "prompt"},
        )
        workflow.retry_stage(run, "FG", self.admin.profile)
        consumer.refresh_from_db()
        self.assertEqual(consumer.status, "E")
        self.assertEqual(consumer.manual_retries, 1)
        self.assertEqual(consumer.attempts, 4)
        self.assertEqual(consumer.input_json, {})
        self.assertEqual(consumer.request_json, {})

    def test_preflight_infrastructure_failure_still_blocks_paid_calls(self):
        run = self.make_run()
        with patch(
            "judge.tasks.coordinator.stage_definitions",
            side_effect=coordinator_stage_definitions,
        ), patch(
            "judge.tasks.coordinator.execute_stage",
            side_effect=CoordinatorInfrastructureError("preflight service unavailable"),
        ):
            for _ in range(3):
                with patch("judge.tasks.coordinator._send_stage") as dispatch:
                    advance_review("problem", run.pk)
                self.assertEqual(dispatch.call_args.args[2], "D")
                run.coordinator_stages.filter(stage_id="D").update(retry_at=None)
                run_review_stage("problem", run.pk, "D")
            with patch("judge.tasks.coordinator._send_stage") as dispatch:
                advance_review("problem", run.pk)
            dispatch.assert_not_called()
        self.assertFalse(
            run.coordinator_stages.exclude(stage_id="D").filter(attempts__gt=0).exists()
        )

    def test_invalidated_execution_claim_cannot_make_a_provider_call(self):
        run = self.make_run(status="B")
        original_claim = uuid.uuid4()
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="H1", status="P", lease_token=None
        )
        run._review_stage_claim = (stage.pk, original_claim)
        with patch("judge.review.coordinator.LLMService") as provider:
            with self.assertRaises(CoordinatorInfrastructureError):
                _call(run, "H1", {"kind": "problem", "available_refs": []})
        provider.assert_not_called()

    def test_invalid_solution_response_retry_explains_missing_ref(self):
        run = self.make_run(status="R")
        context = {
            "kind": "problem",
            "available_refs": ["statement", "solution:1"],
            "solutions": [{"ref": "solution:1", "source": "print(1)"}],
        }
        valid = {
            "status": "no_issue",
            "summary": "Analysis",
            "findings": [],
            "uncertainties": [],
            "solutions": [
                {
                    "ref": "solution:1",
                    "algorithm": "Constant",
                    "correctness_domain": "One",
                    "complexity": "O(1)",
                    "inferred_role": "full",
                    "supported_groups": [],
                    "counterexamples": [],
                    "evidence_refs": ["solution:1"],
                }
            ],
        }
        invalid = json.loads(json.dumps(valid))
        del invalid["solutions"][0]["ref"]
        invalid["summary"] = "UNTRUSTED_RESPONSE_INSTRUCTION"
        with self.assertRaises(CoordinatorOutputError) as caught:
            validate_output(invalid, "H2_BLIND", context)
        self.assertEqual(caught.exception.feedback["path"], "response.solutions[0]")
        self.assertEqual(caught.exception.feedback["fields"], ["ref"])

        frozen = {
            "system_prompt": "Frozen instructions",
            "user_prompt": json.dumps({"untrusted_evidence": context}),
            "model": "test-model",
            "parameters": {},
            "timeout": 120,
            "prompt_version": "cp-coordinator-1",
        }
        stage = ReviewStage.objects.create(
            problem_run=run,
            stage_id="H2_BLIND",
            status="R",
            attempts=2,
            error_category="invalid_response",
            input_json=context,
            request_json=frozen,
        )
        previous = ReviewStageAttempt.objects.create(
            stage=stage,
            number=1,
            error="CoordinatorOutputError: missing fields",
            raw_response=json.dumps(invalid),
            diagnostics_expires_at=timezone.now(),
        )
        current = ReviewStageAttempt.objects.create(
            stage=stage,
            number=2,
            diagnostics_expires_at=timezone.now(),
        )
        with patch("judge.review.coordinator.LLMService") as provider:
            provider.return_value.call_llm.return_value = json.dumps(valid)
            provider.return_value.timeout = 120
            result = _call(run, "H2_BLIND", context)
        self.assertEqual(result["solutions"][0]["ref"], "solution:1")
        self.assertEqual(
            provider.return_value.call_llm.call_args.kwargs["system_prompt"],
            frozen["system_prompt"],
        )
        self.assertEqual(
            provider.call_args.kwargs["metadata"]["prompt_version"],
            frozen["prompt_version"],
        )
        prompt = json.loads(provider.return_value.call_llm.call_args.args[0])
        self.assertEqual(prompt["required_solution_reference_ids"], ["solution:1"])
        self.assertEqual(
            prompt["response_correction"]["validation_error"]["fields"], ["ref"]
        )
        self.assertNotIn("UNTRUSTED_RESPONSE_INSTRUCTION", json.dumps(prompt))
        stage.refresh_from_db()
        current.refresh_from_db()
        previous.refresh_from_db()
        self.assertEqual(stage.request_json, frozen)
        self.assertEqual(provider.call_args.kwargs["timeout"], 480)
        self.assertEqual(json.loads(current.raw_request)["timeout"], 480)
        self.assertEqual(json.loads(current.raw_request)["transport_policy_version"], 2)
        self.assertEqual(
            json.loads(current.raw_request)["streaming_timeouts"],
            {"connect": 20, "first_output": 180, "idle": 120, "total": 480},
        )
        self.assertEqual(
            json.loads(current.raw_request)["user_prompt"],
            provider.return_value.call_llm.call_args.args[0],
        )
        self.assertEqual(json.loads(previous.raw_response), invalid)
        provider.return_value.call_llm.assert_called_once()

        valid["solutions"][0]["ref"] = "invented"
        with self.assertRaises(CoordinatorOutputError):
            validate_output(valid, "H2_BLIND", context)

    def test_recovery_during_response_correction_cannot_start_provider_call(self):
        run = self.make_run(status="R")
        token = uuid.uuid4()
        stage = ReviewStage.objects.create(
            problem_run=run,
            stage_id="H1",
            status="R",
            attempts=1,
            lease_token=token,
        )
        ReviewStageAttempt.objects.create(
            stage=stage, number=1, diagnostics_expires_at=timezone.now()
        )
        run._review_stage_claim = (stage.pk, token)

        def invalidate(*args):
            ReviewStage.objects.filter(pk=stage.pk).update(
                lease_token=None, request_json={}
            )

        with patch(
            "judge.review.coordinator._response_correction", side_effect=invalidate
        ), patch("judge.review.coordinator.LLMService") as provider:
            with self.assertRaises(CoordinatorInfrastructureError):
                _call(run, "H1", {"kind": "problem", "available_refs": []})
        provider.assert_not_called()

    def test_late_response_after_recovery_keeps_original_attempt_context(self):
        run = self.make_run(
            status="B", snapshot={**self.snapshot, "statement": "Original statement"}
        )
        ReviewStage.objects.create(problem_run=run, stage_id="D", status="D")
        ReviewStage.objects.create(
            problem_run=run, stage_id="FG", status="E", attempts=3
        )
        stage = ReviewStage.objects.create(problem_run=run, stage_id="H1", status="P")

        def late_response(current_run, stage_id, context):
            workflow.retry_stage(run, "FG", self.admin.profile)
            return {"summary": "Old generation response"}

        with patch(
            "judge.tasks.coordinator.stage_definitions",
            side_effect=coordinator_stage_definitions,
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=late_response):
            run_review_stage("problem", run.pk, "H1")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "P")
        self.assertEqual(stage.output_json, {})
        receipt = json.loads(stage.attempt_records.get().raw_request)
        self.assertEqual(receipt["statement"], "Original statement")
        self.assertTrue(receipt["missing_evidence"])

    def test_shared_lifetime_credits_ignore_historical_runs(self):
        self.make_run(workflow_version=0, status="D", credit_consumed=True)
        for actor in (self.author, self.other, self.author):
            run = self.make_run(triggered_by=actor.profile, status="D")
            workflow.settle(run)
        self.assertEqual(workflow.remaining_credits(self.problem), 0)
        with self.assertRaises(ValidationError):
            self.start(self.other, True)

    def test_superuser_does_not_consume_credits(self):
        run = self.make_run(
            triggered_by=self.admin.profile, trigger_is_superuser=True, status="D"
        )
        workflow.settle(run)
        self.assertFalse(run.credit_consumed)
        self.assertEqual(workflow.remaining_credits(self.problem), 3)

    def test_completion_notification_is_idempotent_and_failure_recoverable(self):
        run = self.make_run(status="D")
        with patch(
            "judge.tasks.coordinator.emit_review_notification",
            side_effect=RuntimeError("notification unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                emit_completion("problem", run.pk)
        run.refresh_from_db()
        self.assertFalse(run.notifications_sent)
        with patch("judge.tasks.coordinator.emit_review_notification") as notify:
            emit_completion("problem", run.pk)
            emit_completion("problem", run.pk)
        notify.assert_called_once_with(
            "problem", run.problem, run.pk, readiness=run.readiness
        )
        run.refresh_from_db()
        self.assertTrue(run.notifications_sent)

    def test_completion_notifies_all_authors_not_unrelated_superusers(self):
        for kind, obj, model, category in (
            (
                "problem",
                self.problem,
                ProblemReviewRun,
                NotificationCategory.PUBLIC_REQUEST_REVIEW_DONE,
            ),
            (
                "contest",
                self.contest,
                ContestReviewRun,
                NotificationCategory.CONTEST_PUBLIC_REQUEST_REVIEW_DONE,
            ),
        ):
            with self.subTest(kind=kind):
                obj.authors.add(self.other.profile)
                run = model.objects.create(
                    **{kind: obj},
                    triggered_by=self.admin.profile,
                    workflow_version=2,
                    status="D",
                    readiness="READY",
                )
                emit_completion(kind, run.pk)
                emit_completion(kind, run.pk)
                notices = Notification.objects.filter(category=category)
                self.assertCountEqual(
                    notices.values_list("owner_id", flat=True),
                    [self.author.profile.pk, self.other.profile.pk],
                )
                self.assertFalse(
                    Notification.objects.filter(owner=self.admin.profile).exists()
                )
                for notice in notices:
                    self.assertIn("?run_id=%s" % run.pk, notice.html_link)
                    self.assertEqual(notice.extra_data["readiness"], "READY")
                # A superuser who is an author receives the author notification.
                obj.authors.add(self.admin.profile)
                next_run = model.objects.create(
                    **{kind: obj},
                    triggered_by=None,
                    workflow_version=2,
                    status="D",
                    readiness="MANUAL_REVIEW",
                )
                emit_completion(kind, next_run.pk)
                self.assertEqual(
                    Notification.objects.filter(
                        category=category, owner=self.admin.profile
                    ).count(),
                    1,
                )
                Notification.objects.all().delete()

    def test_unfinished_and_cancelled_reviews_send_no_completion_notification(self):
        for status in ("R", "B", "C", "E"):
            with self.subTest(status=status):
                run = self.make_run(status=status)
                emit_completion("problem", run.pk)
                self.assertFalse(Notification.objects.exists())
                run.refresh_from_db()
                self.assertFalse(run.notifications_sent)

    def test_actor_role_at_trigger_is_immutable_for_accounting(self):
        run = self.make_run(status="D", trigger_is_superuser=False)
        self.author.is_superuser = True
        workflow.settle(run)
        self.assertTrue(run.credit_consumed)
        self.author.is_superuser = False

    def test_unchanged_needs_confirmation_but_can_run(self):
        self.make_run(status="D")
        with self.assertRaises(ValidationError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "unchanged")
        run = self.start(confirm=True)
        self.assertEqual(run.workflow_version, 2)
        self.assertEqual(run.status, "R")

    def test_other_author_sees_current_run_claim(self):
        self.make_run(status="B")
        with self.assertRaises(ValidationError):
            self.start(self.other, True)

    def test_missing_preparation_creates_no_run_and_spends_nothing(self):
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ), patch(
            "judge.review.workflow.preparation_errors",
            return_value=["missing validator"],
        ):
            with self.assertRaises(ValidationError):
                workflow.start_problem_review(self.problem, self.author.profile)
        self.assertEqual(self.problem.review_runs.count(), 0)
        self.assertEqual(workflow.remaining_credits(self.problem), 3)

    def test_broken_snapshot_is_preparation_error_not_server_error(self):
        for error in (
            SnapshotError("Corrupt testcase ZIP"),
            FileNotFoundError("missing"),
        ):
            with self.subTest(error=type(error).__name__), patch(
                "judge.review.workflow.build_problem_snapshot", side_effect=error
            ):
                with self.assertRaises(ValidationError) as caught:
                    workflow.start_problem_review(self.problem, self.author.profile)
                self.assertEqual(caught.exception.code, "preparation")
        self.assertEqual(self.problem.review_runs.count(), 0)

    def test_changed_confirmation_creates_no_run(self):
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ):
            with self.assertRaises(ValidationError):
                workflow.start_problem_review(
                    self.problem,
                    self.author.profile,
                    confirm_unchanged=True,
                    expected_content_hash="changed",
                )
        self.assertEqual(self.problem.review_runs.count(), 0)

    def test_invalid_contest_selection_is_validation_error(self):
        with self.assertRaises(ValidationError):
            workflow.start_contest_review(
                self.contest, self.author.profile, selected_problem_ids=["invalid"]
            )

    def test_contest_partial_selection_shared_balances_and_superuser_bypass(self):
        self.addCleanup(Contest._author_ids.dirty, self.contest)
        problems = [self.problem]
        for suffix in ("y", "z", "w"):
            problem = Problem.objects.create(
                code="selection_" + suffix,
                name="Selection " + suffix,
                group_id=self.problem.group_id,
                time_limit=1,
                memory_limit=65536,
                points=100,
                description="Local fixture",
            )
            problem.authors.add(self.author.profile, self.other.profile)
            ContestProblem.objects.create(
                contest=self.contest, problem=problem, points=100, order=len(problems)
            )
            problems.append(problem)
        x, y, z, w = problems
        historical = {}
        for problem, charges in zip(problems, (0, 2, 3, 0)):
            snapshot = build_problem_manifest(problem, include_execution=True)
            for index in range(max(charges, 1)):
                historical[problem.pk] = ProblemReviewRun.objects.create(
                    problem=problem,
                    triggered_by=(self.author if index % 2 else self.other).profile,
                    workflow_version=2,
                    status="D",
                    readiness="READY",
                    snapshot=snapshot,
                    content_hash=snapshot["content_hash"],
                    input_hash=snapshot["input_hash"],
                    credit_consumed=index < charges,
                    credit_settled=True,
                )
        self.contest.authors.add(self.other.profile)
        Contest._author_ids.dirty(self.contest)
        with patch("judge.review.workflow.preparation_errors", return_value=[]), patch(
            "judge.review.workflow._prepared_problem_snapshot",
            side_effect=lambda problem: build_problem_manifest(
                problem, include_execution=True
            ),
        ):
            parent = workflow.start_contest_review(
                self.contest, self.author.profile, selected_problem_ids=[x.pk, y.pk]
            )
            deps = {
                dep.problem_id: dep
                for dep in parent.child_dependencies.select_related("problem_run")
            }
            self.assertEqual(
                {pk for pk, dep in deps.items() if dep.owned}, {x.pk, y.pk}
            )
            for problem in (z, w):
                self.assertEqual(
                    deps[problem.pk].problem_run_id, historical[problem.pk].pk
                )
            for problem in (x, y):
                run = deps[problem.pk].problem_run
                run.status = "D"
                run.save(update_fields=["status"])
                workflow.settle(run)
            parent.status = "D"
            parent.save(update_fields=["status"])
            workflow.settle(parent)
            self.assertEqual(
                [workflow.remaining_credits(p) for p in problems], [2, 0, 0, 3]
            )
            self.assertEqual(workflow.remaining_credits(self.contest), 2)
            counts = (
                ProblemReviewRun.objects.count(),
                ContestReviewRun.objects.count(),
            )
            with self.assertRaises(ValidationError):
                workflow.start_contest_review(
                    self.contest,
                    self.other.profile,
                    selected_problem_ids=[x.pk, z.pk],
                    confirm_unchanged=True,
                )
            # Failure on Z rolls back any X run already created in this transaction.
            self.assertEqual(
                (ProblemReviewRun.objects.count(), ContestReviewRun.objects.count()),
                counts,
            )
            for _ in range(2):
                ContestReviewRun.objects.create(
                    contest=self.contest,
                    triggered_by=self.other.profile,
                    workflow_version=2,
                    status="D",
                    credit_consumed=True,
                    credit_settled=True,
                )
            self.assertEqual(workflow.remaining_credits(self.contest), 0)
            exempt = workflow.start_contest_review(
                self.contest,
                self.admin.profile,
                selected_problem_ids=[z.pk],
                confirm_unchanged=True,
            )
            child = (
                exempt.child_dependencies.select_related("problem_run")
                .get(problem=z)
                .problem_run
            )
            for run in (child, exempt):
                self.assertTrue(run.trigger_is_superuser)
                workflow.settle(run)
                self.assertFalse(run.credit_consumed)
            self.assertEqual(workflow.remaining_credits(z), 0)
            self.assertEqual(workflow.remaining_credits(self.contest), 0)

    def test_contest_modal_uses_translated_results_and_matching_workflow(self):
        self.make_run(status="D", readiness="READY", content_hash="a" * 64)
        # A legacy report is not the report the coordinator will bind.
        ProblemReviewRun.objects.create(
            problem=self.problem, workflow_version=0, status="E"
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get(
                "/reviews/contest/%s/run/" % self.contest.key,
                HTTP_X_REVIEW_FRAGMENT="1",
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready")
        self.assertNotContains(response, ">READY<")
        child = response.context["children"][0]
        self.assertTrue(child["fresh"])
        self.assertFalse(child["selected"])

    def test_contest_publication_request_binds_the_report_shown_not_a_newer_run(self):
        self.client.force_login(self.author)
        old = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
        )
        latest = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
        )
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="a" * 64,
        ), patch(
            "judge.review.publication.workflow.publication_eligibility",
            return_value=True,
        ), patch(
            "judge.review.publication.publication_exposure_concern", return_value=False
        ):
            for run_id in (old.pk, "invalid", 999999):
                response = self.client.post(
                    "/contest/%s/review/request_public" % self.contest.key,
                    {"run_id": run_id},
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(
                    ContestPublicRequest.objects.filter(contest=self.contest).exists()
                )
            response = self.client.post(
                "/contest/%s/review/request_public" % self.contest.key,
                {"run_id": latest.pk},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contest.public_request.review_run_id, latest.pk)
        self.assertFalse(self.contest.is_visible)

    def test_contest_modal_disables_uneditable_problem_and_warns_about_quiz(self):
        self.addCleanup(Problem.get_author_ids.dirty, self.problem)
        self.addCleanup(Problem.dirty_cache, self.problem.pk)
        self.problem.authors.clear()
        quiz = Quiz.objects.create(code="selection_quiz", title="Unsupported quiz")
        ContestProblem.objects.create(
            contest=self.contest, quiz=quiz, points=10, order=1
        )
        self.client.force_login(self.author)
        response = self.client.get(
            "/reviews/contest/%s/run/" % self.contest.key, HTTP_X_REVIEW_FRAGMENT="1"
        )
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertIsNotNone(soup.select_one('input[name="problems"][disabled]'))
        self.assertContains(response, "You do not have permission to run this review.")
        self.assertContains(response, "Unsupported quiz")
        self.assertContains(response, "Unsupported content — manual review required.")
        with self.assertRaises(PermissionDenied):
            workflow.start_contest_review(
                self.contest,
                self.author.profile,
                selected_problem_ids=[self.problem.pk],
            )
        self.assertEqual(ContestReviewRun.objects.count(), 0)

    def test_diagnostics_are_superuser_only_and_not_cached(self):
        run = self.make_run()
        ReviewEvidenceJob.objects.create(
            problem_run=run,
            operation="extractor",
            snapshot_hash=run.input_hash,
            payload_hash="debug",
            payload={"private_binary_payload": "hidden"},
            status="E",
            error="Native worker deadline exceeded",
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="E",
            input_json={"secret_package": "private"},
            output_json={"technical_output_marker": "retained_for_debugging"},
        )
        url = "/reviews/problem/%s/%s/debug/" % (self.problem.code, run.pk)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertContains(response, "secret_package")
        self.assertContains(response, "Structured evidence and notes")
        self.assertContains(response, "technical_output_marker")
        self.assertContains(response, "Native worker deadline exceeded")
        self.assertNotContains(response, "private_binary_payload")

    def test_run_confirmation_renders_in_both_languages(self):
        self.client.force_login(self.author)
        url = "/reviews/problem/%s/run/" % self.problem.code
        for language in ("en", "vi"):
            with override(language), self.subTest(language=language):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'name="expected_content_hash"')
                self.assertNotContains(response, "%(count)s")
                soup = BeautifulSoup(response.content, "html.parser")
                confirmation = soup.select_one('input[name="confirm"]')
                self.assertEqual(confirmation["type"], "hidden")
                self.assertEqual(confirmation["value"], "yes")
                if language == "en":
                    self.assertContains(
                        response, "Start a new AI review of the current content?"
                    )

    def test_run_modal_returns_only_form_and_preserves_full_page_fallback(self):
        self.client.force_login(self.author)
        for kind, obj in (("problem", self.problem), ("contest", self.contest)):
            code = obj.code if kind == "problem" else obj.key
            url = "/reviews/%s/%s/run/" % (kind, code)
            with self.subTest(kind=kind):
                page = self.client.get(url)
                fragment = self.client.get(url, HTTP_X_REVIEW_FRAGMENT="1")
                self.assertEqual(fragment.status_code, 200)
                self.assertNotContains(fragment, "<!DOCTYPE html>")
                self.assertNotContains(fragment, '<div id="nav-container"')
                self.assertContains(fragment, 'class="review-start-form"')
                self.assertNotContains(fragment, "Go back")
                self.assertIn("no-store", fragment["Cache-Control"])
                self.assertIn("X-Review-Fragment", fragment["Vary"])
                self.assertLess(len(fragment.content), len(page.content))
                normal_form = BeautifulSoup(page.content, "html.parser").select_one(
                    ".review-start-form"
                )
                modal_form = BeautifulSoup(fragment.content, "html.parser").select_one(
                    ".review-start-form"
                )
                for name in ("expected_content_hash", "confirm"):
                    self.assertEqual(
                        normal_form.select_one('[name="%s"]' % name)["value"],
                        modal_form.select_one('[name="%s"]' % name)["value"],
                    )
                if kind == "contest":
                    self.assertIsNotNone(
                        modal_form.select_one('input[name="problems"]')
                    )
        self.assertEqual(self.problem.review_runs.count(), 0)
        self.assertEqual(self.contest.review_runs.count(), 0)

    def test_live_report_fragment_is_private_and_omits_discussion(self):
        problem_run = self.make_run(status="R")
        contest_run = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="R",
            triggered_by=self.author.profile,
        )
        self.client.force_login(self.author)
        for kind, code, run in (
            ("problem", self.problem.code, problem_run),
            ("contest", self.contest.key, contest_run),
        ):
            with self.subTest(kind=kind):
                ReviewStage.objects.create(
                    **{kind + "_run": run},
                    stage_id="D",
                    status="D",
                    output_json={"complete": True},
                )
                response = self.client.get(
                    "/%s/%s/review" % (kind, code), HTTP_X_REVIEW_LIVE="1"
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "<!DOCTYPE html>")
                self.assertContains(response, 'id="review-live-feedback"')
                self.assertContains(response, "1 of 1 processing steps completed")
                self.assertIn("no-store", response["Cache-Control"])
                self.assertIn("X-Review-Live", response["Vary"])
                soup = BeautifulSoup(response.content, "html.parser")
                self.assertIsNone(soup.select_one(".review-comments"))
                self.assertIsNone(soup.select_one("script"))

    def test_progress_poll_omits_internal_output_and_retains_access_checks(self):
        run = self.make_run(status="R")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="D",
            status="D",
            output_json={"internal_marker": "large technical output"},
        )
        url = "/problem/%s/review/status" % self.problem.code
        self.client.force_login(self.author)
        response = self.client.get(url, HTTP_X_REVIEW_PROGRESS="1")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "internal_marker")
        self.assertEqual(
            response.json()["run"]["coordinator_stages"],
            [{"stage_id": "D", "status": "D"}],
        )
        self.client.logout()
        response = self.client.get(url, HTTP_X_REVIEW_PROGRESS="1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.get(
                "/problem/%s/review" % self.problem.code, HTTP_X_REVIEW_LIVE="1"
            ).status_code,
            302,
        )

    def test_description_save_stales_review_not_execution_package(self):
        before = build_problem_manifest(self.problem)
        self.problem.description = "Changed upper bound from 200000 to 300000"
        self.problem.date = timezone.now()
        self.problem.save(update_fields=["description", "date"])
        self.addCleanup(Problem.dirty_cache, self.problem.pk)
        after = build_problem_manifest(self.problem)
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(before["package_hash"], after["package_hash"])

    def test_optional_language_template_capture_and_freshness(self):
        before = build_problem_manifest(self.problem, include_execution=True)
        run = self.make_run(
            snapshot=before,
            content_hash=before["content_hash"],
            status="D",
            readiness="READY",
        )
        template = LanguageTemplate.objects.create(
            problem=self.problem,
            language=self.author.profile.language,
            source="# starter",
        )
        after = build_problem_manifest(self.problem, include_execution=True)
        self.assertEqual(after["language_templates"][0]["source"], "# starter")
        self.assertEqual(before["package_hash"], after["package_hash"])
        workflow.invalidate_readiness(self.problem)
        run.refresh_from_db()
        self.assertTrue(run.current_stale)
        template.delete()
        workflow.invalidate_readiness(self.problem)
        run.refresh_from_db()
        self.assertFalse(run.current_stale)

    def test_new_completed_problem_review_stales_contest_without_starting_review(self):
        child_snapshot = build_problem_manifest(self.problem, include_execution=True)
        child = self.make_run(
            snapshot=child_snapshot,
            content_hash=child_snapshot["content_hash"],
            status="D",
            readiness="READY",
        )
        contest_snapshot = build_contest_snapshot(
            self.contest,
            [{"problem_id": self.problem.pk, "content_hash": child.content_hash}],
        )
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash=contest_snapshot["content_hash"],
            snapshot=contest_snapshot,
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash=child.content_hash,
        )
        newer = self.make_run(
            snapshot=child_snapshot, content_hash=child.content_hash, status="R"
        )
        for status in ("R", "B", "C"):
            newer.status = status
            newer.save(update_fields=["status"])
            workflow.invalidate_readiness(self.problem)
            parent.refresh_from_db()
            self.assertFalse(parent.current_stale, status)
        before_count = ContestReviewRun.objects.count()
        self.enqueue.reset_mock()
        newer.status, newer.readiness = "D", "READY"
        newer.save(update_fields=["status", "readiness"])
        workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertTrue(parent.current_stale)
        self.assertEqual(parent.readiness, "READY")
        self.assertEqual(parent.child_dependencies.get().problem_run_id, child.pk)
        self.assertEqual(ContestReviewRun.objects.count(), before_count)
        self.enqueue.assert_not_called()
        reasons = workflow.review_change_reasons(parent, self.contest)
        self.assertIn(
            "This contest uses an older problem review. Run the contest review again to use the latest result.",
            reasons,
        )
        self.client.force_login(self.author)
        response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertEqual(response.status_code, 200)
        confirmation = self.client.get("/reviews/problem/%s/run/" % self.problem.code)
        self.assertContains(
            confirmation, "Contest reviews will not restart automatically"
        )

    def test_hash_schema_upgrade_preserves_exact_historical_child_binding(self):
        snapshot = build_problem_manifest(self.problem, include_execution=True)
        child = self.make_run(
            snapshot=snapshot,
            content_hash="old-schema-child",
            status="D",
            readiness="READY",
        )
        parent_snapshot = build_contest_snapshot(
            self.contest,
            [{"problem_id": self.problem.pk, "content_hash": child.content_hash}],
        )
        parent_snapshot["content_hash_version"] = 2
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="old-schema-parent",
            snapshot=parent_snapshot,
        )
        dep = ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash=child.content_hash,
        )
        workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertFalse(parent.current_stale)
        self.assertEqual(
            reviewed_content_hash(parent), workflow.current_content_hash(self.contest)
        )
        dep.refresh_from_db()
        self.assertEqual(dep.content_hash, "old-schema-child")
        self.assertEqual(dep.problem_run_id, child.pk)
        self.problem.description = "Changed mathematical rule"
        self.problem.save(update_fields=["description"])
        workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertTrue(parent.current_stale)

    def test_meaningful_result_change_sets_freshness_overlay_not_saved_verdict(self):
        snapshot = build_problem_manifest(self.problem, include_execution=True)
        snapshot["solutions"] = [
            {
                "source": "code",
                "language": "PY3",
                "expected_result": "AC",
                "evidence": {"status": "D", "result": "AC"},
            }
        ]
        run = self.make_run(
            snapshot=snapshot,
            content_hash=reviewed_content_hash(SimpleNamespace(snapshot=snapshot)),
            status="D",
            readiness="READY",
        )
        current = dict(
            snapshot,
            solutions=[
                {
                    **snapshot["solutions"][0],
                    "evidence": {"status": "D", "result": "WA"},
                }
            ],
        )
        with patch(
            "judge.review.workflow.current_content_hash", return_value=run.content_hash
        ), patch("judge.review.workflow.build_problem_manifest", return_value=current):
            workflow.invalidate_readiness(self.problem)
        run.refresh_from_db()
        self.assertTrue(run.current_stale)
        self.assertEqual(run.readiness, "READY")

    def test_new_snapshot_does_not_copy_previous_review_conclusions(self):
        before = build_problem_manifest(self.problem, include_execution=True)
        previous = self.make_run(
            snapshot=before,
            status="D",
            readiness="READY_WITH_WARNINGS",
            summary_report="OUTDATED_FORMAT_WARNING",
        )
        historical_output = {
            "findings": [{"claim": "OUTDATED_FORMAT_WARNING"}],
            "actions": ["Change old headings"],
        }
        stage = ReviewStage.objects.create(
            problem_run=previous,
            stage_id="R",
            status="D",
            output_json=historical_output,
        )
        after = build_problem_manifest(self.problem, include_execution=True)
        self.assertNotIn("previous_assessment", after)
        self.assertNotIn("OUTDATED_FORMAT_WARNING", json.dumps(after))
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertEqual(after["input_hash"], before["input_hash"])
        self.assertEqual(after["package_hash"], before["package_hash"])
        previous.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(previous.summary_report, "OUTDATED_FORMAT_WARNING")
        self.assertEqual(previous.readiness, "READY_WITH_WARNINGS")
        self.assertEqual(previous.snapshot, before)
        self.assertEqual(stage.output_json, historical_output)

    def test_metadata_edits_keep_historical_review_fresh_without_rewriting_hash(self):
        before = build_problem_manifest(self.problem, include_execution=True)
        run = self.make_run(
            snapshot=before, content_hash="old-hash", input_hash="old-input"
        )
        original_input = problem_review_input_hash(before)
        self.problem.name = "Renamed problem"
        self.problem.date = timezone.now()
        self.problem.description = "Test\r\n"
        self.problem.summary = "New preview"
        self.problem.save(update_fields=["name", "date", "description", "summary"])
        self.problem.authors.remove(self.other.profile)
        self.problem.curators.add(self.admin.profile)
        self.problem.testers.add(self.other.profile)
        self.addCleanup(Problem.dirty_cache, self.problem.pk)
        after = build_problem_manifest(self.problem, include_execution=True)
        self.assertEqual(reviewed_content_hash(run), after["content_hash"])
        self.assertEqual(original_input, after["input_hash"])
        workflow.invalidate_readiness(self.problem)
        run.refresh_from_db()
        self.assertFalse(run.current_stale)
        self.assertEqual(run.content_hash, "old-hash")
        self.assertEqual(run.input_hash, "old-input")
        self.assertEqual(run.snapshot, before)
        self.problem.description = "Changed rule"
        self.problem.save(update_fields=["description"])
        workflow.invalidate_readiness(self.problem)
        run.refresh_from_db()
        self.assertTrue(run.current_stale)

    def test_legacy_freshness_still_requires_exact_current_execution_evidence(self):
        before = build_problem_manifest(self.problem, include_execution=True)
        run = self.make_run(
            snapshot=before,
            content_hash="old-hash",
            input_hash="old-input",
            status="D",
            readiness="READY",
        )
        ReviewStage.objects.bulk_create(
            [
                ReviewStage(problem_run=run, stage_id=name, status="D")
                for name in workflow.stage_definitions("problem")
            ]
        )
        after = dict(before, input_hash=problem_review_input_hash(before))
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=after
        ), patch("judge.review.workflow.preparation_errors", return_value=[]), patch(
            "judge.review.workflow.publication_exposure_concern", return_value=False
        ):
            self.assertTrue(workflow.publication_eligibility(run, self.problem))
            after["input_hash"] = "new-record-identity-only"
            self.assertTrue(workflow.publication_eligibility(run, self.problem))
            after["validation"] = {
                "status": "D",
                "passed": False,
                "failed_count": 1,
                "total_cases": 1,
            }
            self.assertFalse(workflow.publication_eligibility(run, self.problem))

    def test_new_contest_can_reuse_historical_problem_review_under_narrow_hash(self):
        before = build_problem_manifest(self.problem, include_execution=True)
        child = self.make_run(
            snapshot=before, content_hash="old-hash", status="D", readiness="READY"
        )
        parent = workflow.start_contest_review(
            self.contest, self.admin.profile, selected_problem_ids=[]
        )
        dep = parent.child_dependencies.get()
        self.assertEqual(dep.problem_run_id, child.pk)
        self.assertEqual(dep.content_hash, reviewed_content_hash(child))
        self.assertFalse(dep.owned)
        self.assertEqual(parent.snapshot["children"][0]["name"], self.problem.name)
        self.assertEqual(parent.snapshot["children"][0]["code"], self.problem.code)

    def test_contest_metadata_and_child_metadata_preserve_historical_freshness(self):
        self.addCleanup(Contest._author_ids.dirty, self.contest)
        self.addCleanup(Problem.dirty_cache, self.problem.pk)
        child_hash = build_problem_manifest(self.problem)["content_hash"]
        before = build_contest_snapshot(
            self.contest, [{"problem_id": self.problem.pk, "content_hash": child_hash}]
        )
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="old broad hash",
            input_hash="original input",
            snapshot=before,
        )
        ContestReviewDependency.objects.create(contest_run=parent, problem=self.problem)
        self.contest.name = "Renamed contest"
        self.contest.summary = "Preview only"
        self.contest.start_time += timedelta(days=3)
        self.contest.end_time += timedelta(days=3)
        self.contest.save()
        self.contest.authors.add(self.other.profile)
        self.contest.testers.add(self.admin.profile)
        workflow.invalidate_readiness(self.contest)
        parent.refresh_from_db()
        self.assertFalse(parent.current_stale)
        self.assertEqual(parent.snapshot, before)
        self.assertEqual(parent.content_hash, "old broad hash")
        self.assertEqual(parent.input_hash, "original input")
        self.problem.name = "Renamed problem"
        self.problem.save(update_fields=["name"])
        workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertFalse(parent.current_stale)
        self.problem.description = "Changed problem rule"
        self.problem.save(update_fields=["description"])
        workflow.invalidate_readiness(self.problem)
        parent.refresh_from_db()
        self.assertTrue(parent.current_stale)
        self.assertEqual(parent.readiness, "READY")

    def test_team_change_does_not_stale_contest_but_live_exposure_still_blocks(self):
        self.contest.testers.add(self.other.profile)
        child_hash = build_problem_manifest(self.problem)["content_hash"]
        before = build_contest_snapshot(
            self.contest, [{"problem_id": self.problem.pk, "content_hash": child_hash}]
        )
        self.assertFalse(before["exposure"]["material_concern"])
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash=before["content_hash"],
            snapshot=before,
        )
        self.contest.testers.remove(self.other.profile)
        after = build_contest_snapshot(self.contest, before["children"])
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertTrue(after["exposure"]["material_concern"])
        self.assertFalse(workflow.publication_eligibility(parent, self.contest))

    def test_preparation_errors_are_listed_without_start_prompt_or_modal_back_link(
        self,
    ):
        self.client.force_login(self.admin)
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ), patch(
            "judge.review.workflow.preparation_errors",
            return_value=["Run Validation again.", "Run the accepted solution again."],
        ):
            response = self.client.post(
                "/reviews/problem/%s/run/" % self.problem.code,
                {
                    "confirm": "yes",
                    "expected_content_hash": self.snapshot["content_hash"],
                },
                HTTP_X_REVIEW_FRAGMENT="1",
            )
        self.assertEqual(response.status_code, 400)
        soup = BeautifulSoup(response.content, "html.parser")
        alert = soup.select_one('[role="alert"]')
        self.assertIn("Preparation required", alert.get_text())
        self.assertEqual(len(alert.select("li")), 2)
        self.assertNotContains(response, "Start a new AI review", status_code=400)
        self.assertNotContains(response, "Go back", status_code=400)
        self.assertIsNone(soup.select_one('button[type="submit"]'))
        self.assertEqual(self.problem.review_runs.count(), 0)

    def test_confirmation_credit_copy_distinguishes_superusers_and_authors(self):
        for user in (self.admin, self.author):
            self.client.force_login(user)
            for kind, obj in (("problem", self.problem), ("contest", self.contest)):
                code = obj.code if kind == "problem" else obj.key
                with self.subTest(user=user.username, kind=kind):
                    response = self.client.get(
                        "/reviews/%s/%s/run/" % (kind, code),
                        HTTP_X_REVIEW_FRAGMENT="1",
                    )
                    self.assertContains(response, "Author credits: 3/3")
                    if user.is_superuser:
                        self.assertContains(response, "No author credits will be used.")
                        self.assertNotContains(
                            response, "A completed review uses 1 credit"
                        )
                        self.assertNotContains(
                            response, "Selected problems use their own review credits."
                        )
                        if kind == "contest":
                            self.assertContains(
                                response,
                                "Selected problems get a new review without using author credits.",
                            )
                    else:
                        self.assertContains(
                            response, "A completed review uses 1 credit"
                        )

    def test_modal_actions_return_json_without_fetching_dashboard_twice(self):
        self.client.force_login(self.admin)
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ), patch("judge.review.workflow.preparation_errors", return_value=[]):
            response = self.client.post(
                "/reviews/problem/%s/run/" % self.problem.code,
                {
                    "confirm": "yes",
                    "expected_content_hash": self.snapshot["content_hash"],
                },
                HTTP_X_REVIEW_FRAGMENT="1",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "success": True,
                "redirect": "/problem/%s/review" % self.problem.code,
            },
        )
        run = self.problem.review_runs.get()
        response = self.client.post(
            "/reviews/problem/%s/%s/cancel/" % (self.problem.code, run.pk),
            {"confirm": "yes"},
            HTTP_X_REVIEW_FRAGMENT="1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        run.refresh_from_db()
        self.assertFalse(run.credit_consumed)

    def test_superuser_cancellation_preview_matches_original_trigger_credit_policy(
        self,
    ):
        self.client.force_login(self.admin)
        for superuser_started in (False, True):
            run = self.make_run(
                triggered_by=(
                    self.admin.profile if superuser_started else self.author.profile
                ),
                trigger_is_superuser=superuser_started,
            )
            response = self.client.get("/problem/%s/review" % self.problem.code)
            self.assertContains(
                response,
                (
                    "No author credits will be deducted."
                    if superuser_started
                    else "Cancelling will use 1 author review credit."
                ),
            )
            if superuser_started:
                self.assertNotContains(
                    response, "These reviews were started by authors."
                )
            else:
                self.assertContains(response, "These reviews were started by authors.")
            workflow.cancel_review(run, self.admin.profile)
            run.refresh_from_db()
            self.assertEqual(run.credit_consumed, not superuser_started)

    def test_modal_post_still_rejects_changed_package_without_starting_review(self):
        self.client.force_login(self.author)
        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=self.snapshot
        ):
            response = self.client.post(
                "/reviews/problem/%s/run/" % self.problem.code,
                {"confirm": "yes", "expected_content_hash": "outdated"},
                HTTP_X_REVIEW_FRAGMENT="1",
            )
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "The content changed since you opened the confirmation",
            status_code=400,
        )
        self.assertNotContains(response, "<!DOCTYPE html>", status_code=400)
        self.assertContains(response, 'class="review-start-form"', status_code=400)
        self.assertEqual(self.problem.review_runs.count(), 0)
        self.assertEqual(workflow.remaining_credits(self.problem), 3)

    def test_modal_preparation_does_not_bypass_access_checks(self):
        url = "/reviews/problem/%s/run/" % self.problem.code
        self.assertEqual(
            self.client.get(url, HTTP_X_REVIEW_FRAGMENT="1").status_code, 302
        )
        outsider = User.objects.create_user("modal_outsider")
        Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.author.profile.language}
        )
        outsider.user_permissions.add(Permission.objects.get(codename="add_problem"))
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(url, HTTP_X_REVIEW_FRAGMENT="1").status_code, 403
        )

    def test_recovery_and_cancel_use_buttons_without_confirmation_checkboxes(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run, stage_id="S", status="E", error_category="infrastructure"
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        soup = BeautifulSoup(response.content, "html.parser")
        group = soup.select_one("#review-stage-H7")
        self.assertEqual(
            group.get_text().count("Service unavailable; this check could not finish."),
            1,
        )
        self.assertNotIn("Assessment incomplete", group.get_text())
        self.assertNotIn("not every existing problem", group.get_text())
        self.assertEqual(len(group.select('button[type="submit"]')), 1)
        self.assertIsNone(group.select_one('input[type="checkbox"]'))
        self.assertIn(
            "Completed results are kept. Retry only the failed section",
            group.get_text(),
        )
        dialog = soup.select_one("#review-cancel-dialog")
        self.assertNotIn("Keep review", dialog.get_text())
        self.assertIn("No author credits will be deducted.", dialog.get_text())
        self.assertIn(
            "Cancel this review? Completed section results will be kept.",
            dialog.get_text(),
        )
        self.assertNotIn(self.problem.name, dialog.get_text())
        self.assertIsNone(dialog.select_one('input[type="checkbox"]'))
        self.assertEqual(dialog.select_one('input[name="confirm"]')["value"], "yes")
        # A deliberate POST is still required; no access/confirmation checks relaxed.
        url = "/reviews/problem/%s/%s/retry/" % (self.problem.code, run.pk)
        response = self.client.post(url, {"stage_id": "S"})
        self.assertEqual(response.status_code, 400)
        run.refresh_from_db()
        self.assertEqual(run.status, "B")

    def test_unsupported_contest_child_selection_disabled_even_for_superuser(self):
        ProblemData.objects.create(
            problem=self.problem, output_only=True, use_ioi_signature=True
        )
        for actor in (self.author, self.admin):
            self.client.force_login(actor)
            with patch(
                "judge.views.coordinator_review.current_content_hash",
                return_value="a" * 64,
            ):
                response = self.client.get(
                    "/reviews/contest/%s/run/" % self.contest.key
                )
            self.assertEqual(response.status_code, 200)
            self.assertRegex(
                response.content.decode(),
                r'name="problems" value="%s"\s+disabled' % self.problem.pk,
            )
            self.assertContains(response, "Unsupported content")

    def test_unchanged_content_confirmation_is_explicit(self):
        run = self.make_run(status="D")
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/reviews/problem/%s/run/" % self.problem.code)
        self.assertContains(
            response,
            "Unchanged since the last review. Run again for a fresh assessment?",
        )

    def test_reviewed_snapshot_is_team_only_and_immutable(self):
        run = self.make_run(
            snapshot={"kind": "problem", "statement": "Original statement"}
        )
        url = "/reviews/problem/%s/%s/snapshot/" % (self.problem.code, run.pk)
        self.client.force_login(self.author)
        self.problem.description = "Changed statement"
        self.problem.save(update_fields=["description"])
        response = self.client.get(url)
        self.assertContains(response, "Original statement")
        self.assertNotContains(response, "Changed statement")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_tester_role_does_not_grant_author_package_access(self):
        self.problem.authors.remove(self.other.profile)
        self.problem.testers.add(self.other.profile)
        self.assertFalse(
            workflow.is_review_readable(
                Problem.objects.get(pk=self.problem.pk), self.other
            )
        )

    def test_publication_button_requires_setter_qualification(self):
        run = self.make_run(status="D", readiness="READY")
        self.author.user_permissions.remove(
            Permission.objects.get(codename="add_problem")
        )
        user = User.objects.get(pk=self.author.pk)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ), patch(
            "judge.views.coordinator_review.publication_blockers", return_value=[]
        ):
            context = coordinator_context(self.problem, run, user)
        self.assertFalse(context["coordinator_can_submit"])

    def test_superuser_dashboard_renders_snapshot_bound_publication_form(self):
        run = self.make_run(status="D", readiness="MANUAL_REVIEW")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="E",
            status="D",
            output_json={"summary": "Contract notes"},
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, 'action="/internal/request_public"')
        self.assertContains(response, 'name="id" value="%s"' % self.problem.pk)
        self.assertContains(response, 'name="run_id" value="%s"' % run.pk)
        self.assertNotContains(response, "Contract notes")
        self.assertNotContains(response, 'class="review-checks-table"')
        self.assertNotContains(response, "All checks passed — no AI feedback needed.")
        html = response.content.decode()
        self.assertLess(
            html.index("review-summary"), html.index("coordinator-stage-list")
        )

    def test_contest_review_has_no_inline_admin_decision(self):
        self.client.force_login(self.admin)
        url = "/contest/%s/review" % self.contest.key
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Admin decision")
        for version in (1, 2):
            with self.subTest(workflow_version=version):
                run = ContestReviewRun.objects.create(
                    contest=self.contest,
                    triggered_by=self.author.profile,
                    workflow_version=version,
                    status="D",
                    readiness="READY",
                    content_hash="a" * 64,
                    snapshot={"kind": "contest"},
                )
                with patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value=run.content_hash,
                ):
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Admin decision")
                self.assertNotContains(response, "decision-accept")
                self.assertNotContains(response, "decision-reject")
                self.assertNotContains(response, 'name="override_reason"')
                self.assertNotContains(response, "review-admin-decision")
                self.assertContains(response, 'id="run-status"')

    def test_pending_publication_does_not_offer_duplicate_request(self):
        run = self.make_run(status="D", readiness="READY")
        PublicRequest.objects.create(
            problem=self.problem,
            requested_by=self.author.profile,
            review_run=run,
            candidate_hash=run.content_hash,
        )
        problem = Problem.objects.get(pk=self.problem.pk)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            context = coordinator_context(problem, run, self.admin)
        self.assertFalse(context["coordinator_can_submit"])

    def test_readiness_colors_are_independent_of_execution_status(self):
        run = self.make_run(status="D", readiness="READY")
        for readiness, expected in (
            ("READY", "green"),
            ("READY_WITH_WARNINGS", "orange"),
            ("NEEDS_AUTHOR_FIX", "red"),
            ("MANUAL_REVIEW", "orange"),
            ("INCOMPLETE", "orange"),
            ("", "gray"),
        ):
            with self.subTest(readiness=readiness), patch(
                "judge.views.coordinator_review.current_content_hash",
                return_value=run.content_hash,
            ):
                run.readiness = readiness
                context = coordinator_context(self.problem, run, self.admin)
                self.assertEqual(context["coordinator_readiness_class"], expected)
        run.status = "B"
        run.readiness = "INCOMPLETE"
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            context = coordinator_context(self.problem, run, self.admin)
        self.assertEqual(context["coordinator_execution_class"], "red")
        self.assertEqual(context["coordinator_readiness_class"], "orange")

    def test_content_status_has_semantic_badges(self):
        run = self.make_run(status="D", readiness="MANUAL_REVIEW")
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            context = coordinator_context(self.problem, run, self.admin)
        self.assertEqual(context["coordinator_content_class"], "gray")
        PublicRequest.objects.create(
            problem=self.problem,
            requested_by=self.author.profile,
            review_run=run,
            candidate_hash=run.content_hash,
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, 'class="review-status-badge blue">Submitted')
        self.assertContains(
            response, 'class="review-status-badge orange">Manual review'
        )
        problem = Problem.objects.get(pk=self.problem.pk)
        problem.is_public = True
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ), override("vi"):
            context = coordinator_context(problem, run, self.admin)
        self.assertEqual(context["coordinator_content_class"], "green")

    def test_no_review_uses_neutral_pending_badge(self):
        context = coordinator_context(self.problem, None, self.admin)
        self.assertEqual(context["coordinator_readiness_class"], "gray")
        self.assertEqual(context["coordinator_content_class"], "gray")

    @override_settings(LANGUAGE_CODE="vi")
    def test_contest_review_pages_and_selection_use_vietnamese_wording(self):
        self.client.force_login(self.admin)
        self.client.cookies["django_language"] = "vi"
        run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
        )
        for stage_id in ("H1", "H2"):
            ReviewStage.objects.create(
                contest_run=run,
                stage_id=stage_id,
                status="D",
                output_json={"status": "no_issue", "summary": "Chưa phát hiện lỗi."},
            )
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/contest/%s/review" % self.contest.key)
        self.assertContains(response, "Độ khó và sự đa dạng của bộ đề")
        self.assertContains(response, "Quy định và tính nhất quán")
        self.assertContains(response, "Đánh giá bằng AI")
        self.assertNotContains(response, "Difficulty and diversity")
        response = self.client.get(
            "/reviews/contest/%s/run/" % self.contest.key,
            HTTP_X_REVIEW_FRAGMENT="1",
        )
        self.assertContains(response, "Chọn bài để chạy đánh giá mới")
        self.assertContains(response, "Kết quả hiện tại")
        self.assertContains(response, "Các bài còn lại dùng kết quả đánh giá gần nhất.")
        response = self.client.get("/contests/review/?search=no-matching-contest")
        self.assertContains(response, "Đánh giá kỳ thi")
        self.assertContains(
            response, "Không có kết quả đánh giá kỳ thi nào phù hợp với bộ lọc."
        )

    def test_problem_and_contest_actions_stay_inside_compact_header(self):
        self.client.force_login(self.admin)
        self.make_run(status="D", readiness="READY")
        ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
        )
        for path in (
            "/problem/%s/review" % self.problem.code,
            "/contest/%s/review" % self.contest.key,
        ):
            with self.subTest(path=path), patch(
                "judge.views.coordinator_review.current_content_hash",
                return_value="a" * 64,
            ):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "View reviewed snapshot")
            self.assertNotContains(response, "Review diagnostics")
            self.assertNotContains(response, "/debug/")
            html = response.content.decode()
            self.assertIn('id="page-container" class="column-layout"', html)
            header_start = html.index('class="review-card review-header"')
            header_end = html.index("</section>", header_start)
            self.assertLess(
                header_start, html.index('class="review-workspace-actions"')
            )
            self.assertLess(html.index("data-review-start"), header_end)
            self.assertIn('class="action-btn small"', html[header_start:header_end])

    def test_dashboard_tabs_preserve_history_and_escape_findings(self):
        historical = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        ReviewStage.objects.create(problem_run=historical, stage_id="H4", status="D")
        ReviewStage.objects.create(
            problem_run=historical,
            stage_id="R",
            status="D",
            output_json={
                "findings": [
                    {
                        "claim": "<script>bad()</script>",
                        "severity": "blocking",
                        "suggested_probe_or_fix": "Fix the validator boundary.",
                        "evidence_refs": ["stage:H4"],
                    }
                ],
            },
        )
        self.make_run(status="D", readiness="READY")
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get(
                "/problem/%s/review?run=1&tab=sections" % self.problem.code
            )
        for tab in ("overview", "sections", "discussion"):
            self.assertContains(response, 'id="review-tab-%s"' % tab)
            self.assertContains(response, 'id="review-panel-%s"' % tab)
        self.assertContains(response, 'id="review-history-select"')
        self.assertContains(response, "You are viewing a historical run.")
        self.assertContains(response, "This discussion is shared across review runs")
        self.assertContains(response, "&lt;script&gt;bad()&lt;/script&gt;")
        self.assertNotContains(response, "<script>bad()</script>")
        self.assertContains(response, "Fix the validator boundary.")
        self.assertContains(response, 'data-review-section="H4"')
        form = BeautifulSoup(response.content, "html.parser").select_one(
            'form[action="/internal/request_public"]'
        )
        self.assertIsNotNone(form)
        self.assertTrue(form.button.has_attr("disabled"))

    def test_stale_status_is_not_green_ready_for_public_or_private_problems(self):
        run = self.make_run(status="D", readiness="READY")
        self.client.force_login(self.admin)
        for public in (False, True):
            with self.subTest(public=public):
                Problem.objects.filter(pk=self.problem.pk).update(is_public=public)
                Problem.dirty_cache(self.problem.pk)
                with patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value="changed",
                ):
                    response = self.client.get("/problem/%s/review" % self.problem.code)
                soup = BeautifulSoup(response.content, "html.parser")
                outcome = soup.select_one(".review-outcome")
                self.assertEqual(outcome.select_one("h3").get_text(), "Needs re-review")
                self.assertIn("orange", outcome.select_one("h3")["class"])
                self.assertEqual(
                    outcome.select(".review-outcome-fact strong")[-1].get_text(),
                    "Needs re-review",
                )
                self.assertIn(
                    "Result for the reviewed version: Ready", outcome.get_text()
                )
                self.assertEqual(
                    outcome.select_one("p.review-muted strong").get_text(),
                    "Result for the reviewed version:",
                )
                form = soup.select_one('form[action="/internal/request_public"]')
                if public:
                    self.assertIsNone(form)
                else:
                    self.assertIsNotNone(form)
                    self.assertTrue(form.button.has_attr("disabled"))
        run.refresh_from_db()
        self.assertEqual(run.readiness, "READY")

    def test_change_reasons_only_appear_inside_stale_result_cards(self):
        self.client.force_login(self.admin)
        for kind, obj, model in (
            ("problem", self.problem, ProblemReviewRun),
            ("contest", self.contest, ContestReviewRun),
        ):
            run = model.objects.create(
                **{kind: obj},
                triggered_by=self.author.profile,
                workflow_version=2,
                status="D",
                readiness="READY",
                content_hash="a" * 64,
                snapshot={
                    "kind": kind,
                    "roles": {"authors": [], "curators": [], "testers": []},
                },
            )
            for reasons in (
                [],
                ["Reviewed content has changed."],
                [
                    "Reviewed content has changed.",
                    "An included problem has a newer completed review.",
                ],
            ):
                with self.subTest(kind=kind, reasons=reasons), patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value="a" * 64,
                ), patch(
                    "judge.views.coordinator_review.review_change_reasons",
                    return_value=reasons,
                ):
                    code = obj.code if kind == "problem" else obj.key
                    response = self.client.get("/%s/%s/review" % (kind, code))
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.content, "html.parser")
                card = soup.select_one(".review-outcome")
                changes = soup.select("[data-review-changes]")
                self.assertEqual(len(changes), int(bool(reasons)))
                self.assertIsNone(
                    soup.select_one("#review-live-notices [data-review-changes]")
                )
                if reasons:
                    self.assertIsNotNone(card.select_one("[data-review-changes]"))
                    self.assertEqual(
                        card.select_one("h3").get_text(), "Needs re-review"
                    )
                    for reason in reasons:
                        self.assertIn(reason, changes[0].get_text())
                    self.assertIn("Why re-review is needed", changes[0].get_text())
                    self.assertLess(
                        card.get_text().index("Result for the reviewed version:"),
                        card.get_text().index("Why re-review is needed"),
                    )
                    if len(reasons) == 1:
                        self.assertEqual(changes[0].name, "p")
                    else:
                        self.assertEqual(changes[0].name, "details")
                        self.assertFalse(changes[0].has_attr("open"))
                self.assertNotContains(response, "Authors changed;")
                self.assertNotContains(response, "Curators changed;")
                self.assertNotContains(response, "Testers changed;")
            run.refresh_from_db()
            self.assertEqual(run.readiness, "READY")

    def test_contest_overview_access_and_child_reviews_have_matching_cards(self):
        child = self.make_run(status="D", readiness="READY")
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
            snapshot={"kind": "contest"},
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="a" * 64,
        )
        secret = Contest.objects.create(
            key="unrelatedsecret",
            name="Unrelated private contest name",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
        )
        submission = Submission.objects.create(
            user=self.other.profile,
            problem=self.problem,
            language=self.other.profile.language,
            contest_object=secret,
            status="D",
            result="AC",
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/contest/%s/review" % self.contest.key)
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        access = soup.select_one("section.review-card.review-publication-access")
        self.assertIsNotNone(access)
        self.assertIsNotNone(access.select_one("h3.review-section-title"))
        self.assertEqual(
            [heading.get_text(strip=True) for heading in access.select("thead th")],
            ["Account", "Problem or quiz", "Activity"],
        )
        cells = access.select("tbody tr")[0].select("td")
        self.assertEqual(
            [
                line.get_text(strip=True)
                for line in cells[2].find_all("div", recursive=False)
            ],
            ["Problem author", "1 submission"],
        )
        self.assertFalse(
            access.select_one("details.review-access-records").has_attr("open")
        )
        self.assertIn("review_other", access.get_text())
        self.assertIn("Outside this contest", access.get_text())
        self.assertIn("not proof", access.get_text())
        self.assertIsNotNone(
            access.select_one('a[href="/submission/%s"]' % submission.pk)
        )
        self.assertIsNotNone(
            access.select_one('a[href="/problem/%s"]' % self.problem.code)
        )
        reports = soup.select_one("section.review-card.review-problem-reports")
        self.assertIsNotNone(reports)
        self.assertIsNotNone(reports.select_one("h3.review-section-title"))
        self.assertIsNotNone(reports.select_one("table"))
        sections = soup.select("#review-live-feedback > section")
        self.assertEqual(sections[-1], access)
        self.assertNotContains(response, secret.name)
        self.assertNotContains(response, "/contest/unrelatedsecret")
        run_count = ContestReviewRun.objects.filter(contest=self.contest).count()
        self.assertNotIn("No recorded submissions", cells[2].get_text())
        Submission.objects.filter(pk=submission.pk).delete()
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            permission_only = self.client.get("/contest/%s/review" % self.contest.key)
        role_card = BeautifulSoup(permission_only.content, "html.parser").select_one(
            ".review-publication-access"
        )
        self.assertIn("Problem author", role_card.get_text())
        self.assertIn("No recorded submissions", role_card.get_text())
        self.assertIsNone(role_card.select_one(".review-access-records"))
        self.contest.authors.add(self.other.profile)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            cleared = self.client.get("/contest/%s/review" % self.contest.key)
        clean_card = BeautifulSoup(cleared.content, "html.parser").select_one(
            ".review-publication-access"
        )
        self.assertIsNotNone(clean_card)
        self.assertEqual(
            clean_card.select_one(".review-status-badge.green").get_text(strip=True),
            "Pass",
        )
        self.assertIn("No outside-team access concerns found.", clean_card.get_text())
        self.assertIn("does not prove", clean_card.get_text())
        self.assertIsNone(clean_card.select_one("table"))
        self.assertNotIn("Administrator check required", clean_card.get_text())
        newcomer = User.objects.create_user("late_outsider")
        profile, _ = Profile.objects.get_or_create(
            user=newcomer, defaults={"language": self.author.profile.language}
        )
        Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.author.profile.language,
            status="D",
            result="AC",
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            updated = self.client.get("/contest/%s/review" % self.contest.key)
        self.assertContains(updated, "review-publication-access-title")
        self.assertContains(updated, "late_outsider")
        parent.refresh_from_db()
        self.assertEqual(parent.readiness, "READY")
        self.assertEqual(
            ContestReviewRun.objects.filter(contest=self.contest).count(), run_count
        )
        for index in range(6):
            account = User.objects.create_user("access_group_%s" % index)
            member, _ = Profile.objects.get_or_create(
                user=account, defaults={"language": self.author.profile.language}
            )
            Submission.objects.create(
                user=member,
                problem=self.problem,
                language=self.author.profile.language,
                status="D",
                result="AC",
            )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            grouped = self.client.get("/contest/%s/review" % self.contest.key)
        card = BeautifulSoup(grouped.content, "html.parser").select_one(
            ".review-publication-access"
        )
        self.assertEqual(
            len(card.select_one(".review-bound-table").select("tbody tr")), 5
        )
        more = card.select_one("details.review-access-more")
        self.assertIsNotNone(more)
        self.assertFalse(more.has_attr("open"))
        self.assertEqual(len(more.select("tbody tr")), 2)
        self.assertIn("Show more (2)", more.summary.get_text())
        self.assertIn("7 accounts", card.get_text())
        self.assertIn("7 submissions", card.get_text())

    def test_final_findings_link_to_subject_sections_not_preparation(self):
        run = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        for identity in ("D", "E", "H3", "H4", "H7"):
            ReviewStage.objects.create(problem_run=run, stage_id=identity, status="D")
        final = ReviewStage.objects.create(
            problem_run=run,
            stage_id="R_FINAL",
            status="D",
            output_json={
                "findings": [
                    {
                        "claim": "Strengthen the test coverage.",
                        "evidence_refs": ["stage:E", "tests"],
                    },
                    {
                        "claim": "Compare the equivalent problem.",
                        "evidence_refs": ["candidate:123", "stage:H7"],
                    },
                    {
                        "claim": "Fix the validator.",
                        "evidence_refs": ["artifact:testcase_validator", "stage:E"],
                    },
                    {"claim": "General coordinator advice.", "evidence_refs": []},
                ]
            },
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        soup = BeautifulSoup(response.content, "html.parser")
        preparation = soup.select_one("#review-stage-D")
        self.assertNotIn("Strengthen the test coverage.", preparation.get_text())
        cards = soup.select(".review-attention .review-finding")
        self.assertEqual(
            [
                card.select_one("[data-review-section]")["data-review-section"]
                for card in cards[:3]
            ],
            ["H3", "H7", "H4"],
        )
        self.assertIsNone(cards[-1].select_one("[data-review-section]"))
        final.refresh_from_db()
        self.assertEqual(len(final.output_json["findings"]), 4)

    def test_similarity_shows_assessed_candidates_without_internal_retrieval_section(
        self,
    ):
        run = self.make_run(status="D", readiness="MANUAL_REVIEW")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="S",
            status="D",
            output_json={
                "candidates": [
                    {
                        "ref": "candidate:91",
                        "problem_id": 91,
                        "score": 0.98,
                        "statement": "RAW_SECRET",
                    },
                    {
                        "ref": "candidate:92",
                        "problem_id": 92,
                        "code": "private_candidate",
                    },
                    {
                        "ref": "candidate:93",
                        "problem_id": 93,
                        "code": "deleted_candidate",
                    },
                ],
                "model": "internal_embedding_model",
                "query_hash": "internal_hash",
            },
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H7",
            status="D",
            output_json={
                "comparisons": [
                    {
                        "candidate_ref": "candidate:%s" % identity,
                        "relationship": "essentially_equivalent",
                        "confidence": "high",
                        "core_comparison": "Same core operations.",
                    }
                    for identity in (91, 92, 93)
                ],
            },
        )
        public = SimpleNamespace(
            pk=91,
            code="public_candidate",
            name="Sum <script>unsafe()</script>",
            is_public=True,
            is_organization_private=False,
            get_absolute_url=lambda: "/problem/public_candidate",
        )
        private = SimpleNamespace(pk=92, is_public=True, is_organization_private=True)
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ), patch.object(
            Problem, "get_cached_instances", return_value=[public, private]
        ) as loader:
            response = self.client.get("/problem/%s/review" % self.problem.code)
        loader.assert_called_once()
        self.assertEqual(set(loader.call_args.args), {91, 92, 93})
        section = BeautifulSoup(response.content, "html.parser").select_one(
            "#review-stage-H7"
        )
        html = str(section)
        self.assertNotIn("Retrieved problems (3)", html)
        self.assertNotContains(response, 'id="review-stage-S"')
        self.assertNotContains(response, "Supporting evidence")
        self.assertIn('href="/problem/public_candidate"', html)
        self.assertIn("&lt;script&gt;unsafe()&lt;/script&gt;", html)
        self.assertEqual(
            html.count("This retrieved problem is no longer publicly available."), 2
        )
        for hidden in (
            "RAW_SECRET",
            "internal_embedding_model",
            "internal_hash",
            "private_candidate",
            "deleted_candidate",
            "0.98",
        ):
            self.assertNotIn(hidden, html)
        self.assertEqual(section["data-review-group"], "H7")
        self.assertIn("Same core operations.", html)

    def test_retrieval_empty_and_failed_states_are_distinct(self):
        run = self.make_run(status="D", readiness="INCOMPLETE")
        stage = ReviewStage.objects.create(problem_run=run, stage_id="S", status="D")
        self.client.force_login(self.admin)
        for status, output, empty in (
            ("D", {"candidates": []}, True),
            ("E", {}, False),
            ("P", {}, False),
            ("D", {"candidates": "invalid"}, False),
        ):
            with self.subTest(status=status, output=output):
                stage.status, stage.output_json = status, output
                stage.save(update_fields=["status", "output_json"])
                with patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value=run.content_hash,
                ), patch.object(Problem, "get_cached_instances") as loader:
                    response = self.client.get("/problem/%s/review" % self.problem.code)
                loader.assert_not_called()
                self.assertNotContains(response, 'id="review-stage-S"')
                if status == "E":
                    self.assertContains(
                        response, "Service unavailable; this check could not finish."
                    )
                    self.assertNotContains(response, "Assessment incomplete")
                else:
                    self.assertContains(response, "Assessment incomplete")
                with patch.object(Problem, "get_cached_instances"):
                    context = coordinator_context(self.problem, run, self.admin)
                presented = context["coordinator_stages"][0]
                self.assertEqual(presented.ui_retrieval_complete, empty)
                self.assertFalse(presented.ui_candidates)

    def test_grouped_similarity_keeps_failed_search_and_retry_visible(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        ReviewStage.objects.create(problem_run=run, stage_id="S", status="E")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H7",
            status="D",
            output_json={
                "summary": "Partial assessment",
                "_missing_evidence": [{"stage_id": "S"}],
            },
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(len(soup.select(".review-group")), 8)
        group = soup.select_one("#review-stage-H7")
        self.assertTrue(group.has_attr("open"))
        self.assertEqual(
            group.select_one("summary .review-stage-name").get_text(),
            "Problem similarity",
        )
        self.assertEqual(
            group.select_one("summary .review-status-badge").get_text(), "Error"
        )
        self.assertIsNone(group.select_one("#review-stage-S"))
        self.assertEqual(group.select_one('input[name="stage_id"]')["value"], "S")
        self.assertNotContains(response, "Supporting evidence")
        self.assertIn("Partial assessment", group.get_text())
        self.assertNotIn("No concerning match found", group.get_text())

    def test_overview_findings_render_markdown_math_and_sanitize_html(self):
        run = self.make_run(status="D", readiness="MANUAL_REVIEW")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="R_FINAL",
            status="D",
            output_json={
                "findings": [
                    {
                        "claim": r"**Review** the bounds $N \le 200\,000$ for `int`",
                        "reasoning": (
                            r"Assign $a_i = x$, query $[l, r]$ with $N, Q \le 200\,000$."
                            '\n\n**Bounds** <script>alert("unsafe")</script>'
                        ),
                        "suggested_probe_or_fix": r"Check $N = 1$ and **maximum bounds**.",
                    },
                    {
                        "claim": "Alternative recommendation",
                        "recommendation": r"Compare $O(NQ)$ and $O(Q \log N)$.",
                    },
                ]
            },
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        overview = response.content.decode().split('id="review-panel-overview"', 1)[1]
        overview = overview.split('id="review-panel-sections"', 1)[0]
        self.assertIn(r'<span class="arithmatex">\(a_i = x\)</span>', overview)
        self.assertIn(r'<span class="arithmatex">\([l, r]\)</span>', overview)
        self.assertIn(
            r'<span class="arithmatex">\(N, Q \le 200\,000\)</span>', overview
        )
        self.assertIn(r'<span class="arithmatex">\(N = 1\)</span>', overview)
        self.assertIn(r'<span class="arithmatex">\(O(Q \log N)\)</span>', overview)
        self.assertIn("<strong>maximum bounds</strong>", overview)
        self.assertIn("<strong>Bounds</strong>", overview)
        self.assertNotIn('<script>alert("unsafe")</script>', overview)
        soup = BeautifulSoup(overview, "html.parser")
        attention = soup.select_one("section.review-card.review-attention")
        self.assertIsNotNone(attention)
        self.assertEqual(
            attention.select_one("h3.review-section-title").get_text(strip=True),
            "What needs attention",
        )
        self.assertEqual(len(attention.select(":scope > article.review-finding")), 2)
        self.assertFalse(attention.select(".review-card"))
        title = attention.select_one(".review-finding h4")
        self.assertEqual(title.select_one("strong").get_text(), "Review")
        self.assertEqual(title.select_one("code").get_text(), "int")
        self.assertIsNotNone(title.select_one(".arithmatex"))
        self.assertFalse(title.select("div, p"))
        sections = BeautifulSoup(response.content, "html.parser").select_one(
            "#review-panel-sections"
        )
        self.assertIsNone(sections.select_one("#review-stage-D .review-finding"))

    def test_section_renders_reader_feedback_not_internal_summary(self):
        run = self.make_run(status="D", readiness="READY")
        output = {
            "status": "no_issue",
            "summary": "Technical handoff: artifact:testcase_validator job:internal-id",
            "author_feedback": r"**Validator** đã kiểm tra giới hạn $N \le 200\,000$.",
            "findings": [],
            "uncertainties": [],
        }
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="H4", status="D", output_json=output
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "<strong>Validator</strong>", html=False)
        self.assertContains(response, 'class="arithmatex"')
        self.assertNotContains(response, "job:internal-id")
        self.assertNotContains(response, "Technical handoff")
        stage.refresh_from_db()
        self.assertEqual(stage.output_json, output)

    def test_section_feedback_renders_nested_model_lists_without_changing_saved_text(
        self,
    ):
        run = self.make_run(status="D", readiness="READY")
        sources = {
            "H3": "1. **Phạm vi:**\n   - Biên nhỏ.\n   - Biên lớn.\n\n2. **Phân loại:**\n   - Lời giải sai.",
            "H4": "- **Kiểm tra các thao tác:**\n  - Loại $1$.\n  - Loại $2$.\n- **Kết quả:** Hợp lệ.",
        }
        for identity, source in sources.items():
            ReviewStage.objects.create(
                problem_run=run,
                stage_id=identity,
                status="D",
                output_json={"author_feedback": source},
            )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        soup = BeautifulSoup(response.content, "html.parser")
        for identity, root in (("H3", "ol"), ("H4", "ul")):
            group = soup.select_one("#review-stage-" + identity)
            top = group.select_one(".md-typeset > " + root)
            self.assertEqual(len(top.select(":scope > li")), 2)
            self.assertEqual(
                len(top.select(":scope > li > ul > li")), 3 if identity == "H3" else 2
            )
        self.assertEqual(
            {
                stage.stage_id: stage.output_json["author_feedback"]
                for stage in run.coordinator_stages.all()
            },
            sources,
        )

    def test_internal_extraction_steps_are_hidden_for_authors_and_superusers(self):
        run = self.make_run(status="D", readiness="READY")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H3",
            status="D",
            output_json={"summary": "The package covers maximum bounds."},
        )
        for identity in ("P_EXTRACT", "P_EXTRACT_FINAL"):
            ReviewStage.objects.create(
                problem_run=run,
                stage_id=identity,
                status="D",
                output_json={
                    "limitations": [
                        "Ranges describe observed helper metrics, not independent proof of their interpretation/full input consumption."
                    ],
                },
            )
        for user in (self.author, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                with patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value=run.content_hash,
                ):
                    response = self.client.get("/problem/%s/review" % self.problem.code)
                self.assertContains(response, "The package covers maximum bounds.")
                self.assertNotContains(response, "Supporting evidence")
                self.assertNotContains(response, "Test statistics extraction")
                self.assertNotContains(
                    response, "Ranges describe observed helper metrics"
                )
                self.assertNotContains(response, "review-processing-step")
        self.assertEqual(run.coordinator_stages.count(), 3)

    def test_hidden_evidence_keeps_findings_limitations_and_retry_in_main_group(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H3",
            status="D",
            output_json={"summary": "Available test coverage assessment."},
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="P_EXTRACT",
            status="E",
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="P_EXTRACT_FINAL",
            status="D",
            output_json={"available": False},
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H2_BLIND",
            status="D",
            output_json={
                "findings": [
                    {
                        "claim": "This solution misses an overflow case.",
                        "severity": "warning",
                    }
                ],
                "uncertainties": ["The intended integer range needs clarification."],
            },
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash",
            return_value=run.content_hash,
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        soup = BeautifulSoup(response.content, "html.parser")
        group = soup.select_one("#review-stage-H3")
        self.assertEqual(
            group.select_one('input[name="stage_id"]')["value"], "P_EXTRACT"
        )
        self.assertEqual(
            group.get_text().count("Service unavailable; this check could not finish."),
            1,
        )
        self.assertIsNone(group.select_one('input[type="checkbox"]'))
        self.assertEqual(group.select_one('input[name="confirm"]')["value"], "yes")
        self.assertNotContains(response, "Supporting evidence")
        solutions = soup.select_one("#review-stage-H2")
        self.assertIn("This solution misses an overflow case.", solutions.get_text())
        self.assertIn(
            "The intended integer range needs clarification.", solutions.get_text()
        )
        jump = soup.select_one('[data-review-section="H2"]')
        self.assertIsNotNone(jump)

    def test_completion_uses_reader_feedback_for_overview(self):
        run = self.make_run()
        for identity in coordinator_stage_definitions("problem"):
            ReviewStage.objects.create(
                problem_run=run,
                stage_id=identity,
                status="D",
                output_json={
                    "readiness": "READY",
                    "summary": "Internal technical handoff",
                    "author_feedback": "Chưa phát hiện lỗi trong các phần đã kiểm tra.",
                    "findings": [],
                },
            )
        with patch("judge.tasks.coordinator.emit_completion"):
            advance_review("problem", run.pk)
        run.refresh_from_db()
        self.assertEqual(
            run.summary_report, "Chưa phát hiện lỗi trong các phần đã kiểm tra."
        )

    def test_overview_limits_cards_and_uses_final_not_provisional_findings(self):
        run = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H3",
            status="D",
            output_json={
                "findings": [{"claim": "Provisional observation"}],
            },
        )
        final = ReviewStage.objects.create(
            problem_run=run,
            stage_id="R_FINAL",
            status="D",
            output_json={
                "findings": [
                    {"claim": "Issue %s" % i, "stage": "not a stage"} for i in range(10)
                ],
            },
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            context = coordinator_context(self.problem, run, self.admin)
        self.assertEqual(context["coordinator_findings_count"], 10)
        self.assertEqual(len(context["coordinator_findings"]), 8)
        self.assertFalse(context["coordinator_findings_provisional"])
        self.assertEqual(context["coordinator_findings"][0]["stage"], final)

    def test_dashboard_handles_missing_and_malformed_optional_evidence(self):
        self.client.force_login(self.admin)
        response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "No review yet")
        self.assertContains(response, "data-review-start")
        self.assertNotContains(response, 'id="review-tab-overview"')
        self.assertNotContains(response, 'class="review-card review-attention"')
        run = self.make_run(status="B", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run, stage_id="E", status="E", output_json={"findings": None}
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "Review needs recovery")
        self.assertContains(response, 'data-review-dialog="review-cancel-dialog"')

    def test_running_partial_dashboard_explains_missing_evidence(self):
        self.client.force_login(self.admin)
        run = self.make_run(status="R", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="S",
            status="E",
            reason="Private provider traceback must not be shown",
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H3",
            status="D",
            output_json={
                "status": "no_issue",
                "summary": "Partial source review: 1. Boundary `N`. 2. Stress tests.",
                "_missing_evidence": [{"stage_id": "FG"}],
            },
        )
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H7",
            status="D",
            output_json={"availability": "unavailable"},
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "Other sections are still running.")
        self.assertContains(response, 'review-status-badge orange">Partial')
        self.assertContains(response, "Unavailable upstream evidence")
        self.assertNotContains(response, "Unavailable — missing required inputs")
        self.assertNotContains(response, "Private provider traceback")
        self.assertContains(response, "<ol>")
        self.assertContains(response, "<code>N</code>")
        self.assertContains(response, 'review-status-badge red">Error')
        self.assertContains(
            response, "Service unavailable; this check could not finish."
        )

    def test_invalid_response_label_is_not_an_author_defect(self):
        self.client.force_login(self.admin)
        run = self.make_run(status="B", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="H2_BLIND",
            status="E",
            error_category="invalid_response",
            reason="Private traceback",
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "The AI response was incomplete or invalid.")
        self.assertContains(response, 'review-status-badge red">Error')
        self.assertNotContains(response, 'review-status-badge red">Fail')
        self.assertNotContains(response, "Private traceback")

    def test_section_uses_readable_evidence_not_raw_json(self):
        run = self.make_run(status="D", readiness="READY")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="D",
            status="D",
            output_json={"complete": True, "errors": [], "policy_version": 1},
        )
        self.client.force_login(self.author)
        url = "/problem/%s/review" % self.problem.code
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get(url)
        self.assertContains(response, "All required preparation checks passed.")
        self.assertNotContains(response, "Structured evidence and notes")
        self.assertNotContains(response, '"policy_version": 1')
        self.assertContains(response, 'review-status-badge green">Pass')
        self.assertNotContains(response, "Review diagnostics")
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get(url)
        self.assertNotContains(response, "Structured evidence and notes")
        self.assertNotContains(response, '"policy_version": 1')
        self.addCleanup(self.problem.authors.add, self.other.profile)
        self.addCleanup(self.problem.testers.remove, self.other.profile)
        self.problem.authors.remove(self.other.profile)
        self.problem.testers.add(self.other.profile)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_readable_evidence_keeps_fixes_and_limitations_and_sanitizes_markup(self):
        run = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="D",
            status="D",
            output_json={
                "complete": False,
                "errors": ["Run the validator before reviewing."],
                "limitations": ["Only a subset of tests was examined."],
                "uncertainties": ["<script>unsafe()</script> Unverified case."],
                "internal_only": "technical_output_marker",
            },
        )
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(response, "Run the validator before reviewing.")
        self.assertContains(response, "Only a subset of tests was examined.")
        self.assertContains(response, "Unverified case.")
        self.assertNotContains(response, "<script>unsafe()")
        self.assertNotContains(response, "technical_output_marker")

    def test_polling_endpoints_preserve_dashboard_stage_order(self):
        self.client.force_login(self.admin)
        problem_run = self.make_run(status="B", readiness="INCOMPLETE")
        contest_run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="B",
            readiness="INCOMPLETE",
        )
        for run, relation, path in (
            (
                problem_run,
                "problem_run",
                "/problem/%s/review/status" % self.problem.code,
            ),
            (
                contest_run,
                "contest_run",
                "/contest/%s/review/status" % self.contest.key,
            ),
        ):
            with self.subTest(relation=relation):
                for stage_id, status in (("D", "D"), ("S", "E"), ("E", "P")):
                    ReviewStage.objects.create(
                        **{relation: run}, stage_id=stage_id, status=status
                    )
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                data = response.json()["run"]
                self.assertEqual(data["id"], run.pk)
                self.assertEqual(
                    [stage["stage_id"] for stage in data["coordinator_stages"]],
                    ["D", "S", "E"],
                )
                self.assertEqual(
                    [stage["status"] for stage in data["coordinator_stages"]],
                    ["D", "E", "P"],
                )

    @override_settings(USE_ML=False)
    def test_blocked_dashboard_explains_disabled_search_without_leaking_diagnostics(
        self,
    ):
        self.client.force_login(self.author)
        run = self.make_run(status="B", readiness="INCOMPLETE")
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="S", status="E", error_category="infrastructure"
        )
        ReviewStageAttempt.objects.create(
            stage=stage, number=1, error="private exception and provider details"
        )
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        self.assertContains(
            response,
            "Similarity search is disabled. Ask an administrator to enable it.",
        )
        self.assertContains(response, "No credit is used while recovery is pending.")
        self.assertContains(response, 'data-review-section="S"')
        self.assertNotContains(response, "private exception and provider details")
        self.assertNotContains(response, "Review diagnostics")
        html = BeautifulSoup(response.content, "html.parser")
        button = next(
            b
            for b in html.select("button")
            if b.get_text(strip=True) == "Request publication"
        )
        self.assertTrue(button.has_attr("disabled"))
        self.assertContains(response, "Resolve the failed infrastructure checks")

    def test_publication_action_stays_visible_with_specific_disabled_reason(self):
        self.client.force_login(self.author)
        cases = (
            (None, None, "Run a review before requesting publication."),
            (
                "D",
                "NEEDS_AUTHOR_FIX",
                "Fix the issues identified in this review, then run it again.",
            ),
            (
                "D",
                "MANUAL_REVIEW",
                "An administrator must resolve the flagged findings first.",
            ),
            ("R", "", "Wait for the review to finish."),
            ("C", "", "This review was cancelled. Complete a new review first."),
        )
        for status, readiness, reason in cases:
            if status:
                self.make_run(status=status, readiness=readiness)
            with self.subTest(status=status, readiness=readiness), patch(
                "judge.views.coordinator_review.current_content_hash",
                return_value="a" * 64,
            ):
                response = self.client.get("/problem/%s/review" % self.problem.code)
                self.assertContains(response, reason)
                self.assertNotContains(response, "Publication needs attention")
                html = BeautifulSoup(response.content, "html.parser")
                button = next(
                    b
                    for b in html.select("button")
                    if b.get_text(strip=True) == "Request publication"
                )
                self.assertTrue(button.has_attr("disabled"))
                wrapper = button.parent
                self.assertTrue(wrapper.has_attr("data-publication-hint"))
                self.assertEqual(wrapper.get("tabindex"), "0")
                tooltip = wrapper.select_one('[role="tooltip"]')
                self.assertTrue(tooltip.has_attr("hidden"))
                self.assertEqual(tooltip.get_text(strip=True), reason)
        self.make_run(status="D", readiness="READY")
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ), patch(
            "judge.views.coordinator_review.publication_blockers", return_value=[]
        ):
            response = self.client.get("/problem/%s/review" % self.problem.code)
        button = next(
            b
            for b in BeautifulSoup(response.content, "html.parser").select("button")
            if b.get_text(strip=True) == "Request publication"
        )
        self.assertFalse(button.has_attr("disabled"))
        self.assertNotContains(response, "data-publication-hint")

    def test_contest_live_publication_blockers_appear_on_review_and_content(self):
        run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
            snapshot={"kind": "contest"},
        )
        self.client.force_login(self.author)
        with patch(
            "judge.review.workflow.current_content_hash", return_value="a" * 64
        ), patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ), patch(
            "judge.review.workflow.publication_exposure_concern", return_value=True
        ):
            for path in (
                "/contest/%s/review" % self.contest.key,
                "/contest/%s/edit" % self.contest.key,
            ):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertNotContains(response, 'id="publication-blockers"')
                    self.assertContains(response, 'id="publication-request-hint"')
                    self.assertContains(response, "Possible pre-release access")
        run.refresh_from_db()
        self.assertEqual(run.readiness, "READY")
        self.assertFalse(run.current_stale)

    def test_contest_overview_uses_frozen_names_and_real_difficulty(self):
        child = self.make_run(
            status="D", readiness="READY", snapshot={"name": "Captured problem name"}
        )
        ReviewStage.objects.create(
            problem_run=child,
            stage_id="H6",
            status="D",
            output_json={"difficulty": 1700},
        )
        run = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="MANUAL_REVIEW",
            content_hash="a" * 64,
            snapshot={
                "kind": "contest",
                "items": [{"problem_id": self.problem.pk}, {"quiz_id": 7}],
                "quiz_content": {"quizzes": [{"id": 7, "name": "Captured quiz"}]},
            },
        )
        ContestReviewDependency.objects.create(
            contest_run=run, problem=self.problem, problem_run=child, owned=True
        )
        self.client.force_login(self.admin)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/contest/%s/review" % self.contest.key)
        self.assertContains(response, "Captured problem name")
        self.assertContains(response, "1700")
        self.assertContains(response, "Captured quiz")
        self.assertContains(response, "Unsupported content — manual review required.")
        self.assertContains(response, "Started by this run")
        self.assertContains(response, "run_id=%s" % child.pk)
        html = BeautifulSoup(response.content, "html.parser")
        table = html.select_one(
            "#review-panel-overview .review-problem-reports .review-bound-table"
        )
        self.assertIsNotNone(table)
        self.assertEqual(
            [heading.get_text(strip=True) for heading in table.select("thead th")],
            [
                "Problem",
                "Estimated difficulty",
                "Readiness",
                "Review source",
                "Auto-review",
            ],
        )
        cells = table.select("tbody tr")[0].select("td")
        self.assertEqual(cells[0].a["href"], "/problem/%s" % self.problem.code)
        self.assertEqual(cells[0].a.get_text(strip=True), "Captured problem name")
        self.assertEqual(
            cells[4].a["href"],
            "/problem/%s/review?run_id=%s" % (self.problem.code, child.pk),
        )
        self.assertEqual(cells[4].a.get_text(strip=True), "View review ↗")
        self.assertNotContains(response, "Newer review available")
        self.assertIsNone(html.select_one("#review-panel-sections .review-bound-table"))

    def test_contest_links_newer_child_review_without_rebinding_captured_result(self):
        captured = self.make_run(status="D", readiness="READY")
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
            snapshot={"kind": "contest", "items": [{"problem_id": self.problem.pk}]},
        )
        dependency = ContestReviewDependency.objects.create(
            contest_run=parent, problem=self.problem, problem_run=captured
        )
        self.client.force_login(self.admin)
        for status, readiness in (("R", ""), ("D", "NEEDS_AUTHOR_FIX")):
            with self.subTest(status=status):
                latest = self.make_run(status=status, readiness=readiness)
                with patch(
                    "judge.views.coordinator_review.current_content_hash",
                    return_value="a" * 64,
                ):
                    response = self.client.get("/contest/%s/review" % self.contest.key)
                table = BeautifulSoup(response.content, "html.parser").select_one(
                    ".review-problem-reports .review-bound-table"
                )
                links = table.select("tbody tr td:last-child a")
                self.assertEqual(len(links), 2)
                self.assertIn("run_id=%s" % captured.pk, links[0]["href"])
                self.assertIn("run_id=%s" % latest.pk, links[1]["href"])
                self.assertEqual(links[0].get_text(strip=True), "Review used ↗")
                self.assertEqual(links[1].get_text(strip=True), "Latest review ↗")
                self.assertIn("Not used", table.get_text())
                self.assertEqual(
                    table.select("tbody tr td")[2].get_text(strip=True), "Ready"
                )
                dependency.refresh_from_db()
                parent.refresh_from_db()
                self.assertEqual(dependency.problem_run_id, captured.pk)
                self.assertEqual(parent.readiness, "READY")
                self.assertFalse(parent.current_stale)
                with patch(
                    "judge.views.coordinator_review.is_review_readable",
                    return_value=False,
                ):
                    context = coordinator_context(self.contest, parent, self.admin)
                self.assertIsNone(
                    context["coordinator_child_reports"][0]["newer_review_url"]
                )

    def test_problem_edit_stales_private_and_public_contests_without_rewriting_reports(
        self,
    ):
        self.addCleanup(Problem.dirty_cache, self.problem.pk)
        manifest = build_problem_manifest(self.problem, include_execution=True)
        child = self.make_run(
            snapshot=manifest,
            content_hash=manifest["content_hash"],
            status="D",
            readiness="READY",
        )
        parent = workflow.start_contest_review(self.contest, self.admin.profile)
        parent.status, parent.readiness = "D", "READY"
        parent.save(update_fields=["status", "readiness"])
        frozen = parent.snapshot
        self.problem.description = "Different upper bound and operation"
        self.problem.save(update_fields=["description"])
        for visible in (False, True):
            self.contest.is_visible = visible
            self.contest.save(update_fields=["is_visible"])
            workflow.invalidate_readiness(self.problem)
            parent.refresh_from_db()
            child.refresh_from_db()
            self.assertTrue(parent.current_stale)
            self.assertTrue(child.current_stale)
            self.assertEqual(parent.snapshot, frozen)
            self.assertEqual(parent.readiness, "READY")
            context = coordinator_context(self.contest, parent, self.author)
            self.assertEqual(context["coordinator_state_title"], "Needs re-review")
            self.assertFalse(context["coordinator_can_submit"])
            self.assertEqual(
                context["coordinator_child_reports"][0]["readiness_display"],
                "Needs re-review",
            )
            self.assertEqual(
                context["coordinator_child_reports"][0]["readiness_class"], "orange"
            )

    def test_modal_preparation_keeps_explicit_action_and_quota_gates(self):
        for i in range(3):
            self.make_run(status="D", credit_consumed=True)
        self.client.force_login(self.author)
        with patch(
            "judge.views.coordinator_review.current_content_hash", return_value="a" * 64
        ):
            response = self.client.get("/reviews/contest/%s/run/" % self.contest.key)
        self.assertContains(response, 'class="review-start-form"')
        self.assertContains(
            response, 'action="/reviews/contest/%s/run/"' % self.contest.key
        )
        self.assertContains(response, 'name="problems"')
        self.assertContains(response, "disabled")
        self.enqueue.assert_not_called()

    def test_start_button_respects_active_run_and_shared_credits_not_recovery(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        context = coordinator_context(self.problem, run, self.author)
        self.assertTrue(context["coordinator_can_run"])
        self.assertFalse(context["coordinator_can_start"])
        context = coordinator_context(self.problem, run, self.admin)
        self.assertTrue(context["coordinator_can_start"])
        run.status = "D"
        run.save(update_fields=["status"])
        for _ in range(3):
            finished = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
            workflow.settle(finished)
        context = coordinator_context(self.problem, finished, self.author)
        self.assertTrue(context["coordinator_can_run"])
        self.assertFalse(context["coordinator_can_start"])
        self.assertEqual(context["coordinator_credits"], 0)

    def test_unknown_retry_section_is_validation_error(self):
        run = self.make_run(status="B")
        with self.assertRaises(ValidationError):
            workflow.retry_stage(run, "missing", self.author.profile)

    def test_negative_completed_review_counts_and_settlement_is_idempotent(self):
        run = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        workflow.settle(run)
        workflow.settle(run)
        self.assertEqual(workflow.remaining_credits(self.problem), 2)

    def test_infrastructure_block_cancel_free(self):
        run = self.make_run(status="B", readiness="INCOMPLETE")
        workflow.cancel_review(run, self.other.profile)
        run.refresh_from_db()
        self.assertEqual(run.status, "C")
        self.assertFalse(run.credit_consumed)

    def test_cancel_while_automatic_infrastructure_retry_pending_is_free(self):
        run = self.make_run(status="R")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="E",
            status="P",
            attempts=1,
            error_category="provider",
            retry_at=timezone.now() + timedelta(seconds=5),
        )
        workflow.cancel_review(run, self.author.profile)
        run.refresh_from_db()
        self.assertFalse(run.credit_consumed)

    def test_recovered_temporary_infrastructure_failure_counts_once(self):
        run = self.make_run(status="D")
        ReviewStage.objects.create(
            problem_run=run, stage_id="E", status="D", attempts=2, error_category=""
        )
        workflow.settle(run)
        self.assertTrue(run.credit_consumed)

    def test_healthy_cancel_counts_and_is_terminal(self):
        run = self.make_run()
        workflow.cancel_review(run, self.author.profile)
        run.refresh_from_db()
        self.assertTrue(run.credit_consumed)
        with self.assertRaises(ValidationError):
            workflow.cancel_review(run, self.author.profile)

    def test_cancel_stops_queued_and_running_evidence_only(self):
        run = self.make_run()
        for status in ("P", "R", "D"):
            ReviewEvidenceJob.objects.create(
                problem_run=run,
                operation="extractor",
                snapshot_hash=run.input_hash,
                payload_hash=status,
                status=status,
            )
        workflow.cancel_review(run, self.author.profile)
        self.assertEqual(
            list(run.evidence_jobs.order_by("id").values_list("status", flat=True)),
            ["C", "C", "D"],
        )

    def test_author_retry_once_per_infrastructure_section(self):
        run = self.make_run(status="B")
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="E", status="E", attempts=3
        )
        workflow.retry_stage(run, "E", self.other.profile)
        stage.refresh_from_db()
        self.assertEqual(stage.manual_retries, 1)
        ProblemReviewRun.objects.filter(pk=run.pk).update(status="B")
        ReviewStage.objects.filter(pk=stage.pk).update(status="E")
        with self.assertRaises(PermissionDenied):
            workflow.retry_stage(run, "E", self.author.profile)
        workflow.retry_stage(run, "E", self.admin.profile)

    def test_completed_negative_section_has_no_retry(self):
        run = self.make_run(status="D", readiness="NEEDS_AUTHOR_FIX")
        with self.assertRaises(ValidationError):
            workflow.retry_stage(run, "E", self.author.profile)

    def test_contest_cancel_only_owns_unfinished_created_children(self):
        owned = self.make_run()
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            snapshot={},
            input_hash="x",
            content_hash="x",
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=owned,
            owned=True,
            content_hash="x",
        )
        # Parent ownership still permits cancellation when child edit roles
        # changed after admission; it does not permit new child review work.
        self.problem.authors.clear()
        workflow.cancel_review(parent, self.author.profile)
        owned.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual((parent.status, owned.status), ("C", "C"))
        self.assertTrue(parent.credit_consumed and owned.credit_consumed)

    def test_contest_cancel_does_not_cancel_attached_run(self):
        attached = self.make_run()
        parent = ContestReviewRun.objects.create(
            contest=self.contest,
            triggered_by=self.author.profile,
            workflow_version=2,
            snapshot={},
            input_hash="x",
            content_hash="x",
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=attached,
            owned=False,
            content_hash="x",
        )
        workflow.cancel_review(parent, self.author.profile)
        attached.refresh_from_db()
        self.assertEqual(attached.status, "R")

    def test_stage_redelivery_does_not_repeat_success(self):
        run = self.make_run()
        ReviewStage.objects.create(problem_run=run, stage_id="E")
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch(
            "judge.tasks.coordinator.execute_stage", return_value={"findings": []}
        ) as execute:
            run_review_stage("problem", run.pk, "E")
            run_review_stage("problem", run.pk, "E")
        execute.assert_called_once()
        self.assertEqual(run.coordinator_stages.get(stage_id="E").status, "D")

    def test_timeout_persists_specific_reason_and_partial_without_passing(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="E", attempts=2)
        error = CoordinatorInfrastructureError(
            "The AI response stalled before it finished."
        )
        error.error_category = "timeout_idle"
        error.raw_response = '{"unfinished":'
        error.usage_json = {"output_chars": len(error.raw_response)}
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=error):
            run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.error_category, "timeout_idle")
        self.assertEqual(stage.reason, str(error))
        self.assertEqual(stage.output_json, {})
        attempt = stage.attempt_records.get(number=3)
        self.assertEqual(attempt.raw_response, error.raw_response)
        self.assertEqual(attempt.usage_json, error.usage_json)
        context = coordinator_context(self.problem, run, self.author)
        visible_stage = next(
            item for item in context["coordinator_stages"] if item.stage_id == "E"
        )
        self.assertEqual(visible_stage.ui_reason, str(error))

    def test_permanent_provider_failure_does_not_use_automatic_retry_budget(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="E")
        error = CoordinatorInfrastructureError("Credentials were rejected.")
        error.error_category = "provider_auth"
        error.retryable = False
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=error):
            run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.attempts, 1)
        self.assertIsNone(stage.retry_at)
        self.assertEqual(stage.reason, str(error))

    def test_benchmark_export_is_read_only_and_preflight_is_not_fake_infra(self):
        run = self.make_run(status="D", readiness="INCOMPLETE")
        ReviewStage.objects.create(
            problem_run=run,
            stage_id="D",
            status="D",
            output_json={"errors": ["Validation evidence is stale"]},
        )
        ReviewStage.objects.create(problem_run=run, stage_id="E", status="P")
        output = io.StringIO()
        with self.assertNumQueries(2), patch(
            "llm_service.llm_api.LLMService.call_llm"
        ) as api:
            call_command(
                "review_benchmark",
                export=["stale_execution=problem:%s" % run.pk],
                stdout=output,
            )
        api.assert_not_called()
        observation = json.loads(output.getvalue())[0]
        self.assertFalse(observation["infrastructure_failed"])
        self.assertEqual(observation["findings"][0]["ref"], "D:preparation-0")
        self.assertEqual(observation["input_hash"], run.input_hash)

    def test_rate_limit_retry_respects_provider_delay(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="E")
        error = CoordinatorInfrastructureError("Rate limited.")
        error.error_category = "provider_rate_limit"
        error.retryable = True
        error.retry_after = 75
        started = timezone.now()
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=error):
            run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "P")
        self.assertGreaterEqual(stage.retry_at, started + timedelta(seconds=75))

    def test_excessive_provider_delay_stops_instead_of_ignoring_cooldown(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="E")
        error = CoordinatorInfrastructureError("Rate limited.")
        error.error_category = "provider_rate_limit"
        error.retryable = True
        error.retry_after = 7200
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=error):
            run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertGreater(stage.retry_at, timezone.now())
        ProblemReviewRun.objects.filter(pk=run.pk).update(status="B")
        with self.assertRaises(ValidationError):
            workflow.retry_stage(run, "E", self.author.profile)

    def test_cancel_wins_over_late_phase_response(self):
        run = self.make_run()
        ReviewStage.objects.create(problem_run=run, stage_id="E")

        def execute(*args):
            workflow.cancel_review(run, self.author.profile)
            return {"findings": []}

        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch("judge.tasks.coordinator.execute_stage", side_effect=execute):
            run_review_stage("problem", run.pk, "E")
        run.refresh_from_db()
        self.assertEqual(run.status, "C")
        self.assertNotEqual(run.coordinator_stages.get(stage_id="E").status, "D")

    def test_transport_cancellation_checks_run_and_exact_lease(self):
        run = self.make_run(status="R")
        token = uuid.uuid4()
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="E", status="R", lease_token=token
        )
        # Exercise the same predicate on this test's transactional connection;
        # production invokes it in the transport's isolated polling thread.
        with patch("judge.review.coordinator.connection.close"):
            self.assertTrue(_claim_is_active(stage.pk, token))
            self.assertFalse(_claim_is_active(stage.pk, uuid.uuid4()))
            ProblemReviewRun.objects.filter(pk=run.pk).update(status="C")
            self.assertFalse(_claim_is_active(stage.pk, token))

    def test_three_failed_attempts_then_one_manual_attempt(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(problem_run=run, stage_id="E")
        with patch(
            "judge.tasks.coordinator.build_stage_input", return_value={"frozen": True}
        ), patch(
            "judge.tasks.coordinator.execute_stage",
            side_effect=RuntimeError("provider failed"),
        ):
            for number in range(3):
                ReviewStage.objects.filter(pk=stage.pk).update(retry_at=None)
                run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.attempts, 3)
        ProblemReviewRun.objects.filter(pk=run.pk).update(status="B")
        workflow.retry_stage(run, "E", self.author.profile)
        with patch(
            "judge.tasks.coordinator.execute_stage",
            side_effect=RuntimeError("provider failed"),
        ):
            run_review_stage("problem", run.pk, "E")
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.attempts, 4)

    def test_diagnostics_expiry_preserves_required_context_and_holds(self):
        run = self.make_run()
        stage = ReviewStage.objects.create(
            problem_run=run, stage_id="E", input_json={"essential": "keep"}
        )
        expired = ReviewStageAttempt.objects.create(
            stage=stage,
            number=1,
            raw_request="duplicate",
            raw_response="duplicate",
            diagnostics_expires_at=timezone.now() - timedelta(days=1),
        )
        held = ReviewStageAttempt.objects.create(
            stage=stage,
            number=2,
            raw_request="hold",
            diagnostics_expires_at=timezone.now() - timedelta(days=1),
            retained_until=timezone.now() + timedelta(days=1),
        )
        cleanup_review_diagnostics()
        expired.refresh_from_db()
        held.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(expired.raw_request, "")
        self.assertEqual(held.raw_request, "hold")
        self.assertEqual(stage.input_json, {"essential": "keep"})
