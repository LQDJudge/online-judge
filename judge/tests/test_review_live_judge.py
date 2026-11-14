"""Opt-in end-to-end test using a built local judge and a simulated AI provider.

Set REVIEW_LIVE_JUDGE_ROOT to the judge checkout containing its native sandbox.
The test uses an isolated Django test database and local-only bridge ports.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from celery.contrib.testing.worker import start_worker
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings

from judge.bridge.django_handler import DjangoHandler
from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import JudgeList
from judge.bridge.server import Server
from judge.models import Judge, ProblemData, ProblemTestCase
from judge.auto_review.problem_context import build_problem_context
from judge.auto_review.workflow import start_problem_review
from judge.tests import test_review_queue_flow


@unittest.skipUnless(
    os.environ.get("REVIEW_LIVE_JUDGE_ROOT"),
    "Set REVIEW_LIVE_JUDGE_ROOT to a built judge checkout",
)
@override_settings(USE_ML=False)
class LiveReviewBridgeTest(TransactionTestCase):
    setUp = test_review_queue_flow.ReviewQueueFlowTests.setUp
    provider = test_review_queue_flow.ReviewQueueFlowTests.provider
    wait_for = test_review_queue_flow.ReviewQueueFlowTests.wait_for

    def test_live_bridge_judge_extraction(self):
        dirname = tempfile.mkdtemp(prefix="review-bridge-")
        self.addCleanup(shutil.rmtree, dirname, ignore_errors=True)
        root = Path(dirname).resolve()
        package = root / "problems" / self.problem.code
        package.mkdir(parents=True)
        (package / "init.yml").write_text(
            "archive: tests.zip\ntest_cases:\n  - in: 1.in\n    out: 1.out\n  - in: 2.in\n    out: 2.out\n"
        )
        with ZipFile(package / "tests.zip", "w") as archive:
            archive.writestr("1.in", "3\n")
            archive.writestr("1.out", "3\n")
            archive.writestr("2.in", "5\n")
            archive.writestr("2.out", "5\n")
        storage_patch = patch.object(
            ProblemData._meta.get_field("zipfile").storage,
            "location",
            str(root / "problems"),
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        ProblemData.objects.create(
            problem=self.problem, zipfile=self.problem.code + "/tests.zip"
        )
        for index in (1, 2):
            ProblemTestCase.objects.create(
                dataset=self.problem,
                order=index,
                type="C",
                input_file=f"{index}.in",
                output_file=f"{index}.out",
                points=1,
                is_pretest=False,
            )
        Judge.objects.create(name="review-e2e-judge", auth_key="local-test-key")
        judges = JudgeList()
        judge_server = Server([("127.0.0.1", 0)], partial(JudgeHandler, judges=judges))
        django_server = Server(
            [("127.0.0.1", 0)], partial(DjangoHandler, judges=judges)
        )
        judge_port = judge_server.servers[0].server_address[1]
        django_port = django_server.servers[0].server_address[1]
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (judge_server, django_server)
        ]
        for thread in threads:
            thread.start()
        self.addCleanup(judge_server.shutdown)
        self.addCleanup(django_server.shutdown)
        configfile = root / "judge.yml"
        configfile.write_text(
            "id: review-e2e-judge\nkey: local-test-key\nproblem_storage_globs:\n  - "
            + json.dumps(str(root / "problems" / "*"))
            + "\nruntime:\n  python3: "
            + json.dumps(os.environ.get("REVIEW_LIVE_PYTHON", "/usr/bin/python3"))
            + "\n"
        )
        judge_dir = str(Path(os.environ["REVIEW_LIVE_JUDGE_ROOT"]).resolve())
        env = os.environ.copy()
        env["PYTHONPATH"] = judge_dir
        logfile = (root / "judge.log").open("w+")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from dmoj.judge import main; main()",
                "127.0.0.1",
                "-p",
                str(judge_port),
                "-c",
                str(configfile),
                "--no-watchdog",
                "--skip-self-test",
                "-e",
                "PY3",
            ],
            cwd=judge_dir,
            env=env,
            stdout=logfile,
            stderr=subprocess.STDOUT,
        )

        def cleanup_process():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            logfile.close()

        self.addCleanup(cleanup_process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            close_old_connections()
            if Judge.objects.filter(name="review-e2e-judge", online=True).exists():
                break
            if process.poll() is not None:
                break
            time.sleep(0.2)
        if not Judge.objects.filter(name="review-e2e-judge", online=True).exists():
            logfile.flush()
            logfile.seek(0)
            self.fail("Judge did not connect: " + logfile.read()[-5000:])
        context = build_problem_context(self.problem)
        config = type(
            "Config",
            (),
            {
                "api_key": "offline-fixture",
                "get_bot_name_for_review": lambda self: "offline-fixture",
            },
        )()

        def provider(prompt, **kwargs):
            result = json.loads(self.provider(prompt, **kwargs))
            if json.loads(prompt)["untrusted_evidence"]["phase"] == "problem_contract":
                result["evidence"][
                    "helper_source"
                ] = 'import json; print(json.dumps({"n": int(input())}))'
            return json.dumps(result)

        with override_settings(
            BRIDGED_DJANGO_CONNECT=("127.0.0.1", django_port)
        ), patch(
            "judge.auto_review.workflow._prepared_problem_context", return_value=context
        ), patch(
            "judge.auto_review.phases.get_config", return_value=config
        ), patch(
            "judge.auto_review.phases.LLMService"
        ) as llm, patch(
            "judge.auto_review.phases._retrieve",
            return_value={"candidates": [], "scope": "Indexed public problems only"},
        ), patch(
            "judge.auto_review.scheduling.current_app", self.app
        ), patch(
            "judge.bridge.judge_handler.complete_extraction.delay",
            side_effect=lambda *args: self.app.send_task(
                "judge.tasks.coordinator.complete_extraction", args=args
            ),
        ):
            llm.return_value.call_llm.side_effect = provider
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
                result = run.coordinator_stages.get(
                    stage_id="extract_stats"
                ).output_json
                self.assertEqual(result["availability"], "available", result)
                self.assertEqual(
                    [case["metrics"]["n"] for case in result["cases"]], [3, 5]
                )
