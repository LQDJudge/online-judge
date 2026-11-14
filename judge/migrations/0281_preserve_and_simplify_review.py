"""Preserve original reports before contracting the unused coordinator schema."""

from django.db import migrations

PROBLEM_SECTIONS = {
    "artifacts_present": "preparation_review",
    "duplicate_detection": "duplicate_review",
    "solutions_rubric": "solutions_review",
    "checker_correctness": "checker_review",
    "time_limit_headroom": "limits_review",
    "validator_runs_clean": "validator_review",
}
CONTEST_SECTIONS = {
    "problems_reviewed": "problem_reports_review",
    "submission_leak_check": "contest_access_review",
    "quiz_leak_check": "quiz_access_review",
}


def preserve_history(apps, schema_editor):
    alias = schema_editor.connection.alias
    for name in ("ProblemReviewRun", "ContestReviewRun"):
        model = apps.get_model("judge", name)
        if model.objects.using(alias).filter(workflow_version__gt=0).exists():
            raise RuntimeError(
                "Coordinator reports exist. Preserve/export and map them before applying the review schema contraction."
            )
        if model.objects.using(alias).filter(status__in=("R", "B")).exists():
            raise RuntimeError(
                "Drain active reviews before applying the review schema migration."
            )
    for name in ("ReviewStageAttempt", "ReviewEvidenceJob", "PublicationRequestEvent"):
        if apps.get_model("judge", name).objects.using(alias).exists():
            raise RuntimeError(
                "Unexpected %s data: preserve it before applying this migration." % name
            )

    Stage = apps.get_model("judge", "ReviewStage")
    # Capture the original exact contest-to-problem report links before clearing
    # old hashes or removing the many-to-many table.
    ContestRun = apps.get_model("judge", "ContestReviewRun")
    Dependency = apps.get_model("judge", "ContestReviewDependency")
    links = ContestRun.problem_review_runs.through.objects.using(alias).select_related(
        "problemreviewrun"
    )
    seen = set(
        Dependency.objects.using(alias).values_list("contest_run_id", "problem_id")
    )
    rows = []
    for link in links.iterator(chunk_size=1000):
        child = link.problemreviewrun
        key = (link.contestreviewrun_id, child.problem_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            Dependency(
                contest_run_id=key[0],
                problem_id=key[1],
                problem_run_id=child.pk,
                content_hash=child.input_hash,
                owned=False,
                selected=False,
            )
        )
        if len(rows) == 1000:
            Dependency.objects.using(alias).bulk_create(rows)
            rows = []
    if rows:
        Dependency.objects.using(alias).bulk_create(rows)

    for run_name, check_name, owner, mapping in (
        (
            "ProblemReviewRun",
            "ProblemReviewCheckResult",
            "problem_run",
            PROBLEM_SECTIONS,
        ),
        (
            "ContestReviewRun",
            "ContestReviewCheckResult",
            "contest_run",
            CONTEST_SECTIONS,
        ),
    ):
        Run = apps.get_model("judge", run_name)
        Check = apps.get_model("judge", check_name)
        statuses_by_run = {}
        rows = []
        for result in (
            Check.objects.using(alias)
            .select_related("run")
            .only(
                "pk",
                "run_id",
                "check_id",
                "status",
                "reason",
                "details_json",
                "started_at",
                "finished_at",
                "run__input_hash",
            )
            .iterator(chunk_size=1000)
        ):
            statuses_by_run.setdefault(result.run_id, {})[
                result.check_id
            ] = result.status
            output = {
                "source_check_id": result.check_id,
                "source_check_row_id": result.pk,
                "source_check_status": result.status,
                "original_input_hash": result.run.input_hash,
                "feedback": result.reason,
                "findings": [],
                "evidence": result.details_json,
                "assessment": {"S": "pass", "F": "fail", "W": "warning"}.get(
                    result.status, "uncertain"
                ),
            }
            if result.check_id not in mapping:
                output["display_section"] = True
            rows.append(
                Stage(
                    **{owner + "_id": result.run_id},
                    stage_id=mapping.get(result.check_id, "saved_" + result.check_id),
                    status="D",
                    output_json=output,
                    reason=result.reason,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                )
            )
            if len(rows) == 1000:
                Stage.objects.using(alias).bulk_create(rows)
                rows = []
        if rows:
            Stage.objects.using(alias).bulk_create(rows)

        rows = []
        metadata = []
        for run in Run.objects.using(alias).all().iterator(chunk_size=500):
            statuses = statuses_by_run.get(run.pk, {})
            if run.status != "D":
                run.readiness = "INCOMPLETE"
            elif any(value == "F" for value in statuses.values()):
                run.readiness = "NEEDS_AUTHOR_FIX"
            elif not set(mapping).issubset(statuses) or any(
                statuses[key] not in ("S", "W") for key in mapping
            ):
                run.readiness = "INCOMPLETE"
            elif "W" in statuses.values():
                run.readiness = "READY_WITH_WARNINGS"
            else:
                run.readiness = "READY"
            if not statuses:
                metadata.append(
                    Stage(
                        **{owner + "_id": run.pk},
                        stage_id="saved_metadata",
                        status="D",
                        output_json={
                            "_conversion_metadata": True,
                            "original_input_hash": run.input_hash,
                        },
                    )
                )
            run.input_hash = ""
            run.current_stale = True
            rows.append(run)
            if len(rows) == 500:
                Run.objects.using(alias).bulk_update(
                    rows, ["readiness", "input_hash", "current_stale"]
                )
                rows = []
            if len(metadata) == 500:
                Stage.objects.using(alias).bulk_create(metadata)
                metadata = []
        if rows:
            Run.objects.using(alias).bulk_update(
                rows, ["readiness", "input_hash", "current_stale"]
            )
        if metadata:
            Stage.objects.using(alias).bulk_create(metadata)


def restore_original_checks(apps, schema_editor):
    alias = schema_editor.connection.alias
    Stage = apps.get_model("judge", "ReviewStage")
    for run_name, check_name, owner in (
        ("ProblemReviewRun", "ProblemReviewCheckResult", "problem_run"),
        ("ContestReviewRun", "ContestReviewCheckResult", "contest_run"),
    ):
        Run = apps.get_model("judge", run_name)
        Check = apps.get_model("judge", check_name)
        rows, restored_hashes, converted_ids = [], {}, []
        for stage in (
            Stage.objects.using(alias)
            .filter(**{owner + "__isnull": False})
            .iterator(chunk_size=1000)
        ):
            value = stage.output_json
            if not isinstance(value, dict):
                continue
            if value.get("_conversion_metadata") is True:
                restored_hashes[getattr(stage, owner + "_id")] = value.get(
                    "original_input_hash", ""
                )
            elif "source_check_status" in value and "source_check_id" in value:
                restored_hashes[getattr(stage, owner + "_id")] = value.get(
                    "original_input_hash", ""
                )
                rows.append(
                    Check(
                        id=value.get("source_check_row_id"),
                        run_id=getattr(stage, owner + "_id"),
                        check_id=value["source_check_id"],
                        status=value["source_check_status"],
                        reason=stage.reason,
                        details_json=value.get("evidence", {}),
                        started_at=stage.started_at,
                        finished_at=stage.finished_at,
                    )
                )
            else:
                continue
            converted_ids.append(stage.pk)
            if len(rows) == 1000:
                Check.objects.using(alias).bulk_create(rows, ignore_conflicts=True)
                rows = []
        if rows:
            Check.objects.using(alias).bulk_create(rows, ignore_conflicts=True)
        run_ids = list(restored_hashes)
        for start in range(0, len(run_ids), 500):
            runs = list(
                Run.objects.using(alias).filter(pk__in=run_ids[start : start + 500])
            )
            for run in runs:
                run.input_hash = restored_hashes[run.pk]
            Run.objects.using(alias).bulk_update(runs, ["input_hash"], batch_size=500)
        # Remove converted rows, so a later forward migration cannot duplicate them.
        for start in range(0, len(converted_ids), 1000):
            Stage.objects.using(alias).filter(
                pk__in=converted_ids[start : start + 1000]
            ).delete()

    ContestRun = apps.get_model("judge", "ContestReviewRun")
    Dependency = apps.get_model("judge", "ContestReviewDependency")
    through = ContestRun.problem_review_runs.through
    links = [
        through(contestreviewrun_id=contest_run_id, problemreviewrun_id=problem_run_id)
        for contest_run_id, problem_run_id in Dependency.objects.using(alias)
        .filter(owned=False, problem_run__isnull=False)
        .values_list("contest_run_id", "problem_run_id")
        .iterator(chunk_size=1000)
    ]
    if links:
        through.objects.using(alias).bulk_create(
            links, ignore_conflicts=True, batch_size=1000
        )


class Migration(migrations.Migration):
    dependencies = [("judge", "0280_review_freshness_and_phase_state")]
    operations = [
        migrations.RunPython(preserve_history, restore_original_checks),
        # MySQL cannot drop this FK column while its explicit compound unique
        # constraint still references it. The autodetector omits this operation
        # when deleting the model after its FK; remove the constraint first.
        migrations.RemoveConstraint(
            model_name="reviewevidencejob",
            name="review_evidence_job_identity",
        ),
        migrations.AlterUniqueTogether(
            name="problemreviewcheckresult",
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name="problemreviewcheckresult",
            name="run",
        ),
        migrations.RemoveField(
            model_name="publicationrequestevent",
            name="actor",
        ),
        migrations.RemoveField(
            model_name="publicationrequestevent",
            name="contest",
        ),
        migrations.RemoveField(
            model_name="publicationrequestevent",
            name="contest_run",
        ),
        migrations.RemoveField(
            model_name="publicationrequestevent",
            name="problem",
        ),
        migrations.RemoveField(
            model_name="publicationrequestevent",
            name="problem_run",
        ),
        migrations.RemoveField(
            model_name="reviewevidencejob",
            name="problem_run",
        ),
        migrations.AlterUniqueTogether(
            name="reviewstageattempt",
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name="reviewstageattempt",
            name="stage",
        ),
        migrations.RemoveField(
            model_name="contestreviewdependency",
            name="selected",
        ),
        migrations.RemoveField(
            model_name="contestreviewrun",
            name="content_hash",
        ),
        migrations.RemoveField(
            model_name="contestreviewrun",
            name="problem_review_runs",
        ),
        migrations.RemoveField(
            model_name="contestreviewrun",
            name="snapshot",
        ),
        migrations.RemoveField(
            model_name="contestreviewrun",
            name="workflow_version",
        ),
        migrations.RemoveField(
            model_name="problemreviewrun",
            name="content_hash",
        ),
        migrations.RemoveField(
            model_name="problemreviewrun",
            name="snapshot",
        ),
        migrations.RemoveField(
            model_name="problemreviewrun",
            name="workflow_version",
        ),
        migrations.RemoveField(
            model_name="reviewstage",
            name="delivery_attempts",
        ),
        migrations.RemoveField(
            model_name="reviewstage",
            name="input_json",
        ),
        migrations.RemoveField(
            model_name="reviewstage",
            name="lease_expires_at",
        ),
        migrations.RemoveField(
            model_name="reviewstage",
            name="request_json",
        ),
        migrations.DeleteModel(
            name="ContestReviewCheckResult",
        ),
        migrations.DeleteModel(
            name="ProblemReviewCheckResult",
        ),
        migrations.DeleteModel(
            name="PublicationRequestEvent",
        ),
        migrations.DeleteModel(
            name="ReviewEvidenceJob",
        ),
        migrations.DeleteModel(
            name="ReviewStageAttempt",
        ),
    ]
