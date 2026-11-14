"""Current workflow behavior: real phase rows, mocked external AI/broker only."""

import uuid
import io
import tempfile
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from judge.models import (
    Contest,
    ContestProblem,
    ContestReviewDependency,
    ContestReviewRun,
    Language,
    Problem,
    ProblemGroup,
    ProblemData,
    ProblemTestCase,
    ProblemReviewRun,
    Profile,
    ReviewStage,
)
from judge.auto_review import workflow
from judge.auto_review.problem_context import build_problem_context
from judge.auto_review.review_context import current_content_hash
from judge.auto_review.phases import (
    CoordinatorInfrastructureError,
    CoordinatorOutputError,
    effective_readiness,
    stage_definitions,
    validate_output,
)
from judge.auto_review.scheduling import _dispatch, schedule_phases
from judge.tasks.coordinator import (
    run_review_stage,
    _finish,
    reap_review_runs,
    refresh_review_freshness,
)


class PhaseContractTests(SimpleTestCase):
    def test_generator_only_omits_optional_work_and_edge(self):
        graph = stage_definitions("problem", {"artifacts": {"generator": {}}})
        self.assertNotIn("extract_stats", graph)
        self.assertNotIn("extract_stats", graph["test_review"]["dependencies"])
        graph = stage_definitions(
            "problem", {"artifacts": {"zipfile": {"filename": "tests.zip"}}}
        )
        self.assertIn("extract_stats", graph["test_review"]["dependencies"])

    def test_all_edges_point_to_existing_phases(self):
        for kind in ("problem", "contest"):
            graph = stage_definitions(kind, {"artifacts": {"zipfile": True}})
            self.assertTrue(
                all(
                    set(value["dependencies"]) <= set(graph) for value in graph.values()
                )
            )

    def test_similarity_requires_comparison_for_every_candidate(self):
        output = {
            "assessment": "pass",
            "feedback": "Không thấy vấn đề.",
            "findings": [],
            "evidence": {"comparisons": []},
        }
        with self.assertRaises(CoordinatorOutputError):
            validate_output(
                output, "similarity_review", {"candidates": [{"ref": "candidate:1"}]}
            )
        self.assertEqual(
            validate_output(output, "similarity_review", {"candidates": []}), output
        )

    def test_bad_findings_and_readiness_are_not_accepted(self):
        for extra in ({"findings": [{"severity": "pass"}]}, {"readiness": "DONE"}):
            output = {
                "assessment": "pass",
                "feedback": "Feedback",
                "findings": [],
                "evidence": {},
                "readiness": "READY",
                **extra,
            }
            with self.assertRaises(CoordinatorOutputError):
                validate_output(output, "final_assessment", {})


@override_settings(USE_ML=False, LANGUAGE_CODE="en")
class ReviewEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        user = User.objects.create_superuser(
            "engine_admin", "engine@example.invalid", "test"
        )
        cls.actor, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": language}
        )
        author_user = User.objects.create_user("engine_author", password="test")
        author_user.user_permissions.add(Permission.objects.get(codename="add_problem"))
        cls.author, _ = Profile.objects.get_or_create(
            user=author_user, defaults={"language": language}
        )
        group = ProblemGroup.objects.create(name="engine", full_name="Engine tests")
        cls.problem = Problem.objects.create(
            code="engine1",
            name="Engine",
            description="Original statement",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        cls.problem.authors.add(cls.author)
        cls.contest = Contest.objects.create(
            key="enginecontest",
            name="Engine contest",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
        )
        cls.contest.authors.add(cls.author)
        ContestProblem.objects.create(
            contest=cls.contest, problem=cls.problem, order=0, points=10
        )

    def context(self):
        return {
            "kind": "problem",
            "input_hash": "identity",
            "content_hash": "identity",
            "artifacts": {},
            "statement": "Statement",
        }

    def run_record(self, *, kind="problem", author=False):
        model = ProblemReviewRun if kind == "problem" else ContestReviewRun
        obj = self.problem if kind == "problem" else self.contest
        run = model.objects.create(
            **{kind: obj},
            triggered_by=self.author if author else self.actor,
            input_hash="identity",
            trigger_is_superuser=not author
        )
        ReviewStage.objects.bulk_create(
            [
                ReviewStage(**{kind + "_run": run}, stage_id=key)
                for key in stage_definitions(kind)
            ]
        )
        return run

    def complete(self, run, key, output=None):
        ReviewStage.objects.filter(
            **{("problem_run" if hasattr(run, "problem_id") else "contest_run"): run},
            stage_id=key
        ).update(
            status="D",
            output_json=output
            or {
                "assessment": "pass",
                "feedback": "Passed",
                "findings": [],
                "evidence": {},
            },
        )

    def queued(self, run, key):
        stage = run.coordinator_stages.get(stage_id=key)
        stage.status, stage.lease_token = ReviewStage.QUEUED, uuid.uuid4()
        stage.save()
        return stage

    def test_duplicate_scheduling_dispatches_roots_once(self):
        run = self.run_record()
        with patch("judge.auto_review.scheduling._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_phases("problem", run.pk)
                schedule_phases("problem", run.pk)
            self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(run.coordinator_stages.filter(status="Q").count(), 2)

    def test_identity_matches_full_context_without_scanning_zip_or_creating_archive(
        self,
    ):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("1.in", "2\n1 2\n")
            archive.writestr("1.out", "3\n")
        with tempfile.TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root)
            with patch.object(
                ProblemData._meta.get_field("zipfile"), "storage", storage
            ), patch("judge.auto_review.problem_context.problem_data_storage", storage):
                ProblemData.objects.create(
                    problem=self.problem,
                    checker="standard",
                    zipfile=ContentFile(raw.getvalue(), name="tests.zip"),
                )
                ProblemTestCase.objects.create(
                    dataset=self.problem,
                    order=0,
                    type="C",
                    input_file="1.in",
                    output_file="1.out",
                    is_pretest=False,
                )
                context = build_problem_context(self.problem)
                with patch(
                    "judge.auto_review.problem_context._zip_inventory",
                    side_effect=AssertionError("Hash must not inspect test bytes"),
                ), patch(
                    "judge.auto_review.problem_context._read_auxiliary_sources",
                    side_effect=AssertionError("Hash must not reread source trees"),
                ):
                    self.assertEqual(
                        current_content_hash(self.problem), context["input_hash"]
                    )
                self.assertEqual(context["inventory"]["configured_cases"], 1)
                self.assertNotIn("sha256", context["artifacts"]["zipfile"])

    def test_parallel_join_waits_for_all_predecessors_then_claims_once(self):
        run = self.run_record()
        self.complete(run, "independent_analysis")
        with patch("judge.auto_review.scheduling._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_phases("problem", run.pk, "independent_analysis")
            self.assertNotIn(
                "test_review", [call.args[2] for call in dispatch.call_args_list]
            )
            self.complete(run, "solutions_review")
            dispatch.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                schedule_phases("problem", run.pk, "solutions_review")
                schedule_phases("problem", run.pk, "solutions_review")
            self.assertEqual(
                [call.args[2] for call in dispatch.call_args_list], ["test_review"]
            )

    def test_duplicate_task_calls_ai_once(self):
        run = self.run_record()
        stage = self.queued(run, "problem_contract")
        token = stage.lease_token
        with patch("judge.tasks.coordinator.build_stage_input", return_value={}), patch(
            "judge.tasks.coordinator.execute_stage",
            return_value={"feedback": "Complete"},
        ) as execute, patch(
            "judge.tasks.coordinator._accept_identity", return_value=True
        ):
            run_review_stage("problem", run.pk, stage.stage_id, str(stage.lease_token))
            run_review_stage("problem", run.pk, stage.stage_id, str(stage.lease_token))
        self.assertEqual(execute.call_count, 1)
        stage.refresh_from_db()
        self.assertEqual(stage.attempts, 1)
        self.assertEqual(stage.status, "D")

    def test_cancelled_run_rejects_late_output(self):
        run = self.run_record()
        stage = self.queued(run, "problem_contract")
        stage.status = "R"
        stage.save()
        token = stage.lease_token
        workflow.cancel_review(run, self.actor)
        _finish("problem", run.pk, stage.stage_id, str(token), {"feedback": "Late"})
        stage.refresh_from_db()
        self.assertEqual(stage.output_json, {})
        self.assertIsNone(stage.lease_token)

    def test_old_attempt_cannot_overwrite_retry(self):
        run = self.run_record()
        stage = self.queued(run, "problem_contract")
        old = stage.lease_token
        stage.status = "R"
        stage.lease_token = uuid.uuid4()
        stage.save()
        _finish("problem", run.pk, stage.stage_id, str(old), {"feedback": "Late"})
        stage.refresh_from_db()
        self.assertEqual(stage.output_json, {})

    def test_required_failure_retries_twice_then_blocks(self):
        run = self.run_record(author=True)
        stage = self.queued(run, "problem_contract")
        for attempt in range(1, 4):
            stage.status = "R"
            stage.attempts = attempt
            stage.save()
            with patch("judge.tasks.coordinator._accept_identity", return_value=True):
                _finish(
                    "problem",
                    run.pk,
                    stage.stage_id,
                    str(stage.lease_token),
                    error=CoordinatorInfrastructureError("Provider failed"),
                )
            stage.refresh_from_db()
            self.assertEqual(stage.status, "P" if attempt < 3 else "E")
        workflow.settle(run, cancelled=True)
        self.assertFalse(run.credit_consumed)
        with patch("judge.auto_review.scheduling._dispatch"), patch(
            "judge.auto_review.scheduling.current_app.send_task"
        ):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_phases("problem", run.pk, "problem_contract")
        run.refresh_from_db()
        self.assertEqual(run.status, "B")
        self.assertEqual(
            run.coordinator_stages.get(stage_id="independent_analysis").status, "P"
        )

    def test_manual_retry_is_one_attempt_without_automatic_cycle(self):
        run = self.run_record(author=True)
        stage = run.coordinator_stages.get(stage_id="problem_contract")
        stage.status = "E"
        stage.attempts = 3
        stage.save()
        with patch("judge.auto_review.workflow.review_is_current", return_value=True):
            workflow.retry_stage(run, stage.stage_id, self.author)
        stage.refresh_from_db()
        self.assertEqual(stage.manual_retries, 1)
        stage.status = "R"
        stage.lease_token = uuid.uuid4()
        stage.attempts = 4
        stage.save()
        with patch("judge.tasks.coordinator._accept_identity", return_value=True):
            _finish(
                "problem",
                run.pk,
                stage.stage_id,
                str(stage.lease_token),
                error=OSError("Still down"),
            )
        stage.refresh_from_db()
        self.assertEqual(stage.status, "E")
        with patch(
            "judge.auto_review.workflow.review_is_current", return_value=True
        ), self.assertRaises(PermissionDenied):
            workflow.retry_stage(run, stage.stage_id, self.author)

    def test_optional_extraction_unavailable_finishes_and_keeps_normal_credit(self):
        run = self.run_record(author=True)
        stage = ReviewStage.objects.create(
            problem_run=run,
            stage_id="extract_stats",
            status="R",
            attempts=3,
            lease_token=uuid.uuid4(),
        )
        with patch("judge.tasks.coordinator._accept_identity", return_value=True):
            _finish(
                "problem",
                run.pk,
                "extract_stats",
                str(stage.lease_token),
                error=OSError("Judge unavailable"),
            )
        stage.refresh_from_db()
        self.assertEqual(stage.status, "D")
        self.assertEqual(stage.output_json["availability"], "unavailable")
        workflow.settle(run)
        self.assertTrue(run.credit_consumed)

    def test_contest_cancel_only_cancels_owned_unfinished_children(self):
        child = self.run_record()
        parent = self.run_record(kind="contest")
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
            owned=False,
        )
        workflow.cancel_review(parent, self.actor)
        child.refresh_from_db()
        self.assertEqual(child.status, "R")
        parent2 = self.run_record(kind="contest")
        ContestReviewDependency.objects.create(
            contest_run=parent2,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
            owned=True,
        )
        workflow.cancel_review(parent2, self.actor)
        child.refresh_from_db()
        self.assertEqual(child.status, "C")

    def test_problem_save_cancels_affected_parent_even_if_its_child_completed(self):
        child = self.run_record()
        child.status = "D"
        child.save()
        parent = self.run_record(kind="contest")
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
            owned=False,
        )
        with patch("judge.auto_review.workflow.invalidate_readiness"):
            workflow.cancel_for_save(self.problem, self.author.user)
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(parent.status, "C")
        self.assertEqual(child.status, "D")

    def test_execution_result_change_cancels_active_review_and_affected_contest(self):
        child = self.run_record()
        parent = self.run_record(kind="contest")
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
            owned=False,
        )
        with patch(
            "judge.tasks.coordinator.current_content_hash", return_value="changed"
        ), patch("judge.tasks.coordinator.invalidate_readiness"):
            refresh_review_freshness(self.problem.pk)
        child.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(child.status, "C")
        self.assertEqual(parent.status, "C")

    def test_identical_execution_result_keeps_active_review(self):
        run = self.run_record()
        with patch(
            "judge.tasks.coordinator.current_content_hash", return_value="identity"
        ), patch("judge.tasks.coordinator.invalidate_readiness"):
            refresh_review_freshness(self.problem.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "R")

    def test_lost_initial_dispatch_becomes_retryable_infrastructure_error(self):
        run = self.run_record()
        ProblemReviewRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=4)
        )
        reap_review_runs("problem")
        run.refresh_from_db()
        self.assertEqual(run.status, "B")
        failed = run.coordinator_stages.get(status="E")
        self.assertEqual(failed.error_category, "worker_timeout")

    def test_reaper_finalizes_completed_phases_after_lost_callback(self):
        run = self.run_record()
        old = timezone.now() - timedelta(hours=4)
        ProblemReviewRun.objects.filter(pk=run.pk).update(started_at=old)
        run.coordinator_stages.update(status="D", finished_at=old)
        with patch("judge.tasks.coordinator.schedule_phases") as schedule:
            with self.captureOnCommitCallbacks(execute=True):
                reap_review_runs("problem")
        schedule.assert_called_once_with("problem", run.pk)

    def test_public_problem_blocked_before_ai_or_run_creation(self):
        self.problem.is_public = True
        self.problem.save()
        with self.assertRaises(ValidationError), patch(
            "judge.auto_review.workflow.build_problem_context"
        ) as context:
            workflow.start_problem_review(self.problem, self.actor)
        self.assertFalse(context.called)
        self.assertFalse(ProblemReviewRun.objects.exists())

    def test_restricted_visibility_does_not_lock_and_restriction_only_publication_does(
        self,
    ):
        self.problem.is_public = True
        self.problem.is_organization_private = True
        self.problem.save()
        self.assertFalse(self.problem.is_locked)
        self.problem.is_organization_private = False
        self.problem.save(update_fields=["is_organization_private"])
        self.assertTrue(self.problem.is_locked)
        self.problem.is_locked = False
        self.problem.save(update_fields=["is_locked"])
        self.problem.save()
        self.assertFalse(self.problem.is_locked)
        self.contest.is_visible = True
        self.contest.is_private = True
        self.contest.save()
        self.assertFalse(self.contest.is_locked)
        self.contest.is_private = False
        self.contest.save(update_fields=["is_private"])
        self.assertTrue(self.contest.is_locked)

    def test_public_contest_or_child_blocks_before_child_creation(self):
        for parent_public in (True, False):
            self.contest.is_visible = parent_public
            self.contest.save()
            self.problem.is_public = not parent_public
            self.problem.save()
            with patch(
                "judge.auto_review.workflow.current_content_hash",
                return_value="identity",
            ), self.assertRaises(ValidationError):
                workflow.start_contest_review(
                    self.contest,
                    self.actor,
                    selected_problem_ids=[self.problem.pk],
                    confirm_unchanged=True,
                )
            self.assertFalse(ProblemReviewRun.objects.exists())

    def test_later_selected_preparation_error_creates_no_child(self):
        other = Problem.objects.create(
            code="engine2",
            name="Second",
            description="Test",
            group=self.problem.group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        ContestProblem.objects.create(
            contest=self.contest, problem=other, order=1, points=10
        )
        with patch(
            "judge.auto_review.workflow.current_content_hash", return_value="identity"
        ), patch(
            "judge.auto_review.workflow._prepared_problem_context",
            side_effect=[self.context(), ValidationError("Missing validator")],
        ), self.assertRaises(
            ValidationError
        ):
            workflow.start_contest_review(
                self.contest,
                self.actor,
                selected_problem_ids=[self.problem.pk, other.pk],
                confirm_unchanged=True,
            )
        self.assertFalse(ProblemReviewRun.objects.exists())

    def test_newer_completed_child_marks_contest_stale_without_swapping(self):
        child = self.run_record()
        child.status = "D"
        child.save()
        parent = self.run_record(kind="contest")
        dep = ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
            owned=False,
        )
        newer = self.run_record()
        newer.status = "D"
        newer.save()
        with patch("judge.auto_review.workflow.review_is_current", return_value=True):
            reasons = workflow.review_change_reasons(parent, self.contest)
        self.assertTrue(
            any("older problem review" in str(reason) for reason in reasons)
        )
        dep.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(dep.problem_run_id, child.pk)
        self.assertEqual(parent.status, "R")

    def test_timeout_preserves_completed_results_and_invalidates_attempt(self):
        run = self.run_record()
        self.complete(run, "similarity_search")
        stage = self.queued(run, "problem_contract")
        token = stage.lease_token
        ProblemReviewRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=4)
        )
        reap_review_runs("problem")
        stage.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(stage.status, "E")
        self.assertIsNone(stage.lease_token)
        self.assertEqual(run.status, "B")
        self.assertEqual(
            run.coordinator_stages.get(stage_id="similarity_search").status, "D"
        )

    def test_contest_reuse_requires_access_to_problem_review(self):
        other_user = User.objects.create_user("other_contest_author", password="test")
        other_user.user_permissions.add(Permission.objects.get(codename="add_problem"))
        other, _ = Profile.objects.get_or_create(
            user=other_user, defaults={"language": self.actor.language}
        )
        contest = Contest.objects.create(
            key="privatechild",
            name="Private child",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
        )
        contest.authors.add(other)
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, order=0, points=10
        )
        with self.assertRaises(PermissionDenied):
            workflow.start_contest_review(contest, other, selected_problem_ids=[])

        child = self.run_record()
        parent = ContestReviewRun.objects.create(
            contest=contest, triggered_by=self.actor
        )
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
        )
        self.client.force_login(other_user)
        self.assertEqual(
            self.client.get("/contest/privatechild/review").status_code, 403
        )
        self.assertEqual(
            self.client.get("/contest/privatechild/review/status").status_code, 403
        )

    def test_contest_readiness_respects_child_readiness(self):
        child = self.run_record()
        child.status = "D"
        child.readiness = "NEEDS_AUTHOR_FIX"
        child.save()
        parent = self.run_record(kind="contest")
        ContestReviewDependency.objects.create(
            contest_run=parent,
            problem=self.problem,
            problem_run=child,
            content_hash="identity",
        )
        self.complete(parent, "final_assessment", {"readiness": "READY"})
        self.assertEqual(effective_readiness(parent), "NEEDS_AUTHOR_FIX")

    def test_lost_optional_extraction_does_not_block_review(self):
        run = self.run_record()
        old = timezone.now() - timedelta(hours=4)
        ProblemReviewRun.objects.filter(pk=run.pk).update(started_at=old)
        ReviewStage.objects.create(
            problem_run=run, stage_id="extract_stats", status="R", started_at=old
        )
        with patch("judge.tasks.coordinator.schedule_phases") as schedule:
            with self.captureOnCommitCallbacks(execute=True):
                reap_review_runs("problem")
        run.refresh_from_db()
        stage = run.coordinator_stages.get(stage_id="extract_stats")
        self.assertEqual(stage.status, "D")
        self.assertEqual(stage.output_json["availability"], "unavailable")
        self.assertEqual(run.status, "R")
        schedule.assert_called_once_with("problem", run.pk, "extract_stats")

    def test_broker_failure_becomes_retryable_without_hour_wait(self):
        run = self.run_record()
        stage = self.queued(run, "problem_contract")
        with patch(
            "judge.auto_review.scheduling.current_app.send_task",
            side_effect=OSError("broker down"),
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                _dispatch("problem", run.pk, stage.stage_id, stage.lease_token, 0)
        stage.refresh_from_db()
        run.refresh_from_db()
        self.assertGreaterEqual(send.call_count, 3)
        self.assertEqual(stage.status, "E")
        self.assertEqual(stage.error_category, "broker_delivery")
        self.assertEqual(run.status, "B")

    def test_broker_delivery_retries_and_keeps_the_same_claim(self):
        run = self.run_record()
        stage = self.queued(run, "problem_contract")
        token = stage.lease_token
        with patch(
            "judge.auto_review.scheduling.current_app.send_task",
            side_effect=[OSError("broker down"), OSError("broker down"), None],
        ) as send:
            _dispatch("problem", run.pk, stage.stage_id, stage.lease_token, 0)
        stage.refresh_from_db()
        self.assertEqual(send.call_count, 3)
        self.assertEqual(stage.status, "Q")
        self.assertEqual(stage.lease_token, token)
        self.assertTrue(
            all(call.kwargs["args"][-1] == str(token) for call in send.call_args_list)
        )

    def test_save_probe_reports_active_review_without_saving(self):
        run = self.run_record(author=True)
        self.client.force_login(self.author.user)
        response = self.client.post(
            "/problem/engine1/edit",
            {"description": "Changed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_X_REVIEW_SAVE_PROBE="1",
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["requires_review_confirmation"])
        run.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(run.status, "R")
        self.assertEqual(self.problem.description, "Original statement")

    def test_contest_row_preview_does_not_cancel_review(self):
        run = self.run_record(kind="contest", author=True)
        self.client.force_login(self.author.user)
        self.client.post(
            "/contest/enginecontest/edit",
            {"preview_contest_problem_submission_delete": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "R")
