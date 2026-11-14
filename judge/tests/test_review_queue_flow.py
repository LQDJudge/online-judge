"""Exercise the complete Django -> Celery -> DB flow with an offline AI provider."""

import json
import time
from datetime import timedelta
from unittest.mock import patch

from celery import Celery
from celery.contrib.testing.worker import start_worker
from django.contrib.auth.models import Permission, User
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    ReviewStage,
)
from judge.auto_review.problem_context import build_problem_context
from judge.auto_review.workflow import start_problem_review, start_contest_review


@override_settings(USE_ML=False)
class ReviewQueueFlowTests(TransactionTestCase):
    def setUp(self):
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
        user = User.objects.create_user("queue_author")
        user.user_permissions.add(Permission.objects.get(codename="add_problem"))
        self.author, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": language}
        )
        group = ProblemGroup.objects.create(name="queue_flow", full_name="Queue flow")
        self.problem = Problem.objects.create(
            code="queueflow",
            name="Queue workflow",
            description="Given two integers, output their sum.",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        self.problem.authors.add(self.author)
        self.app = Celery(
            "review_flow_test",
            broker="memory://",
            backend="cache+memory://",
            include=["judge.tasks.coordinator"],
        )
        self.app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            task_default_queue="review_flow_test",
            worker_hijack_root_logger=False,
            broker_connection_retry_on_startup=False,
        )
        self.calls = []

    def provider(self, prompt, **kwargs):
        data = json.loads(prompt)
        phase = data["untrusted_evidence"]["phase"]
        self.calls.append(phase)
        result = {
            "assessment": "pass",
            "feedback": "Các kiểm tra đã hoàn tất, chưa phát hiện vấn đề.",
            "findings": [],
            "evidence": {},
        }
        if phase == "problem_contract":
            result["evidence"]["contract"] = {
                "objective": "Sum two integers",
                "input": "Two integers",
                "output": "Their sum",
                "constraints": "Integers fitting 64 bits",
            }
        elif phase == "independent_analysis":
            result["evidence"]["analysis"] = {
                "algorithm": "Add the integers",
                "correctness": "Integer addition",
                "complexity": "O(1)",
            }
        elif phase == "solutions_review":
            result["evidence"]["solutions"] = []
        elif phase == "similarity_review":
            result["evidence"]["comparisons"] = []
        elif phase == "limits_review":
            result["evidence"].update(difficulty=100, rationale="Direct arithmetic")
        elif phase == "final_assessment":
            result["readiness"] = "READY"
        return json.dumps(result, ensure_ascii=False)

    def wait_for(self, run):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            close_old_connections()
            run.refresh_from_db()
            if run.status in ("D", "B", "C", "E"):
                break
            time.sleep(0.1)
        self.assertEqual(
            run.status,
            "D",
            list(run.coordinator_stages.values("stage_id", "status", "reason")),
        )
        self.assertEqual(run.readiness, "READY")
        self.assertTrue(run.credit_consumed)
        self.assertTrue(run.credit_settled)
        self.assertFalse(
            run.coordinator_stages.exclude(status=ReviewStage.DONE).exists()
        )

    def test_problem_and_reused_child_contest_finish_on_real_worker(self):
        # Preparation is independently tested. This test isolates asynchronous
        # scheduling, real contexts/schema parsing and finalization from judging.
        context = build_problem_context(self.problem)
        config = type(
            "Config",
            (),
            {
                "api_key": "offline-fixture",
                "get_bot_name_for_review": lambda self: "offline-fixture",
            },
        )()
        with patch(
            "judge.auto_review.workflow._prepared_problem_context", return_value=context
        ), patch("judge.auto_review.phases.get_config", return_value=config), patch(
            "judge.auto_review.phases.LLMService"
        ) as provider, patch(
            "judge.auto_review.phases._retrieve",
            return_value={"candidates": [], "scope": "Indexed public problems only"},
        ), patch(
            "judge.auto_review.scheduling.current_app", self.app
        ):
            provider.return_value.call_llm.side_effect = self.provider
            with start_worker(
                self.app,
                perform_ping_check=False,
                pool="solo",
                concurrency=1,
                loglevel="ERROR",
                shutdown_timeout=10,
            ):
                run = start_problem_review(
                    self.problem, self.author, confirm_unchanged=True
                )
                self.wait_for(run)
                self.assertEqual(len(self.calls), len(set(self.calls)))
                self.assertNotIn(
                    "extract_stats",
                    set(run.coordinator_stages.values_list("stage_id", flat=True)),
                )
                contest = Contest.objects.create(
                    key="queuecontest",
                    name="Queue contest",
                    start_time=timezone.now(),
                    end_time=timezone.now() + timedelta(hours=2),
                )
                contest.authors.add(self.author)
                ContestProblem.objects.create(
                    contest=contest, problem=self.problem, order=0, points=10
                )
                parent = start_contest_review(
                    contest,
                    self.author,
                    selected_problem_ids=[],
                    confirm_unchanged=True,
                )
                self.wait_for(parent)
                dependency = parent.child_dependencies.get()
                self.assertEqual(dependency.problem_run_id, run.pk)
                self.assertFalse(dependency.owned)
