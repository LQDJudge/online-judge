"""Exercise populated forward migrations on the real database backend."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from judge.models import Language, Profile, ProblemGroup, Problem, ProblemReviewRun
from judge.auto_review.presentation import review_section_groups
from judge.auto_review.workflow import review_change_reasons, review_is_current

ORIGINAL_PROBLEM_CHECKS = (
    "artifacts_present",
    "duplicate_detection",
    "solutions_rubric",
    "checker_correctness",
    "time_limit_headroom",
    "validator_runs_clean",
)


@override_settings(USE_ML=False, LANGUAGE_CODE="en")
class ReviewMigrationTests(TransactionTestCase):
    before = ("judge", "0280_review_freshness_and_phase_state")
    after = ("judge", "0281_preserve_and_simplify_review")

    def setUp(self):
        self.forward_applied = False
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
        user = User.objects.create_user("migration_author")
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": language}
        )
        group = ProblemGroup.objects.create(name="migration", full_name="Migration")
        self.problem = Problem.objects.create(
            code="migrate1",
            name="Original",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.before])
        self.old = self.executor.loader.project_state([self.before]).apps

    def tearDown(self):
        # Always restore runtime schema, even after an assertion or guard failure.
        if not self.forward_applied:
            self.old.get_model("judge", "ProblemReviewRun").objects.filter(
                workflow_version__gt=0
            ).delete()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.after])
        self.forward_applied = True
        super().tearDown()

    def forward(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.after])
        self.forward_applied = True

    def test_checks_links_hashes_and_outcomes_are_preserved(self):
        Run = self.old.get_model("judge", "ProblemReviewRun")
        Check = self.old.get_model("judge", "ProblemReviewCheckResult")
        run = Run.objects.create(
            problem_id=self.problem.pk,
            triggered_by_id=self.profile.pk,
            status="D",
            input_hash="original-identity",
            summary_report="Original summary",
        )
        for key in ORIGINAL_PROBLEM_CHECKS:
            Check.objects.create(
                run=run,
                check_id=key,
                status="S",
                reason="Original feedback",
                details_json={"source": "original"},
            )
        CRun = self.old.get_model("judge", "ContestReviewRun")
        contest = self.old.get_model("judge", "Contest").objects.create(
            key="migratecontest",
            name="Original contest",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
        )
        parent = CRun.objects.create(
            contest=contest, status="D", input_hash="contest-identity"
        )
        parent.problem_review_runs.add(run)
        self.forward()
        # Import uses current schema only after migration has finished.
        apps = self.executor.loader.project_state([self.after]).apps
        current = apps.get_model("judge", "ProblemReviewRun").objects.get(pk=run.pk)
        self.assertEqual(current.input_hash, "")
        self.assertTrue(current.current_stale)
        self.assertEqual(current.readiness, "READY")
        runtime_run = ProblemReviewRun.objects.get(pk=run.pk)
        self.assertFalse(review_is_current(runtime_run, self.problem))
        self.assertEqual(
            review_change_reasons(runtime_run, self.problem),
            ["Run a new review before requesting publication."],
        )
        groups = review_section_groups(list(runtime_run.coordinator_stages.all()))
        duplicate_group = next(group for group in groups if group["id"] == "duplicates")
        self.assertEqual(duplicate_group["status"], "Pass")
        self.assertFalse(any(group["id"] == "H7" for group in groups))
        self.assertEqual(current.summary_report, "Original summary")
        phases = list(current.coordinator_stages.all())
        self.assertEqual(len(phases), len(ORIGINAL_PROBLEM_CHECKS))
        self.assertTrue(
            all(s.output_json["evidence"] == {"source": "original"} for s in phases)
        )
        self.assertTrue(
            all(
                s.output_json["original_input_hash"] == "original-identity"
                for s in phases
            )
        )
        self.assertEqual(
            {s.stage_id for s in phases},
            {
                "preparation_review",
                "duplicate_review",
                "solutions_review",
                "checker_review",
                "limits_review",
                "validator_review",
            },
        )
        dependency = apps.get_model("judge", "ContestReviewDependency").objects.get(
            contest_run_id=parent.pk
        )
        self.assertEqual(dependency.problem_run_id, run.pk)
        self.assertEqual(dependency.content_hash, "original-identity")
        self.assertFalse(dependency.owned)
        tables = connection.introspection.table_names()
        for obsolete in (
            "judge_reviewstageattempt",
            "judge_reviewevidencejob",
            "judge_publicationrequestevent",
            "judge_problemreviewcheckresult",
            "judge_contestreviewcheckresult",
        ):
            self.assertNotIn(obsolete, tables)
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.before])
        apps = self.executor.loader.project_state([self.before]).apps
        restored_parent = apps.get_model("judge", "ContestReviewRun").objects.get(
            pk=parent.pk
        )
        self.assertTrue(restored_parent.problem_review_runs.filter(pk=run.pk).exists())
        restored = apps.get_model("judge", "ProblemReviewCheckResult").objects.filter(
            run_id=run.pk
        )
        self.assertEqual(restored.count(), len(ORIGINAL_PROBLEM_CHECKS))
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.after])

    def test_unexpected_coordinator_data_blocks_destructive_migration(self):
        Run = self.old.get_model("judge", "ProblemReviewRun")
        run = Run.objects.create(
            problem_id=self.problem.pk,
            triggered_by_id=self.profile.pk,
            status="D",
            workflow_version=2,
            input_hash="unexpected",
            snapshot={"statement": "Must not disappear"},
        )
        with self.assertRaisesRegex(RuntimeError, "Coordinator reports exist"):
            self.forward()
        old = Run.objects.get(pk=run.pk)
        self.assertEqual(old.snapshot, {"statement": "Must not disappear"})

    def test_failure_and_warning_outcomes_are_preserved(self):
        Run = self.old.get_model("judge", "ProblemReviewRun")
        Check = self.old.get_model("judge", "ProblemReviewCheckResult")
        run_ids = []
        for status in ("F", "W"):
            run = Run.objects.create(
                problem_id=self.problem.pk, status="D", input_hash=status * 64
            )
            run_ids.append(run.pk)
            for key in ORIGINAL_PROBLEM_CHECKS:
                Check.objects.create(
                    run=run,
                    check_id=key,
                    status=status if key == "checker_correctness" else "S",
                )
        self.forward()
        apps = self.executor.loader.project_state([self.after]).apps
        Run = apps.get_model("judge", "ProblemReviewRun")
        self.assertEqual(Run.objects.get(pk=run_ids[0]).readiness, "NEEDS_AUTHOR_FIX")
        self.assertEqual(
            Run.objects.get(pk=run_ids[1]).readiness, "READY_WITH_WARNINGS"
        )

    def test_incomplete_and_missing_checks_remain_incomplete_and_stale(self):
        Run = self.old.get_model("judge", "ProblemReviewRun")
        Check = self.old.get_model("judge", "ProblemReviewCheckResult")
        missing = Run.objects.create(
            problem_id=self.problem.pk, status="D", input_hash="missing-checks"
        )
        partial = Run.objects.create(
            problem_id=self.problem.pk, status="D", input_hash="partial-checks"
        )
        Check.objects.create(
            run=partial, check_id="checker_correctness", status="P", reason="Waiting"
        )
        Check.objects.create(
            run=partial, check_id="validator_runs_clean", status="E", reason="Failed"
        )
        self.forward()
        apps = self.executor.loader.project_state([self.after]).apps
        Run = apps.get_model("judge", "ProblemReviewRun")
        for pk in (missing.pk, partial.pk):
            migrated = Run.objects.get(pk=pk)
            self.assertEqual(migrated.readiness, "INCOMPLETE")
            self.assertTrue(migrated.current_stale)
            self.assertEqual(migrated.input_hash, "")
        for stage in Run.objects.get(pk=partial.pk).coordinator_stages.all():
            self.assertEqual(stage.status, "D")
            self.assertEqual(stage.output_json["assessment"], "uncertain")
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.before])
        apps = self.executor.loader.project_state([self.before]).apps
        reverted = apps.get_model("judge", "ProblemReviewRun").objects.get(
            pk=missing.pk
        )
        self.assertEqual(reverted.input_hash, "missing-checks")
        self.assertFalse(reverted.coordinator_stages.exists())
        restored = apps.get_model("judge", "ProblemReviewCheckResult").objects.filter(
            run_id=partial.pk
        )
        self.assertEqual(
            dict(restored.values_list("check_id", "status")),
            {"checker_correctness": "P", "validator_runs_clean": "E"},
        )
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.after])
