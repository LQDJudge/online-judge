"""Transactional admission, credit settlement and recovery for coordinator runs."""

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import (
    Contest,
    ContestReviewDependency,
    ContestReviewRun,
    Problem,
    ProblemData,
    ProblemReviewRun,
    ReviewEvidenceJob,
    ReviewStage,
)
from judge.review.coordinator import stage_definitions
from judge.review.exposure import publication_exposure_concern
from judge.review.dispatch import dispatch_advance
from judge.review.execution import cancel_evidence_jobs
from judge.review.snapshots import (
    SnapshotError,
    build_contest_snapshot,
    build_problem_snapshot,
    build_problem_manifest,
    current_content_hash,
    preparation_errors,
    problem_review_input_hash,
    reviewed_content_hash,
    execution_results_changed,
    problem_review_content,
    contest_review_content,
    normalized_dependency_hash,
)
from judge.review.types import is_supported_problem

WORKFLOW_VERSION = 2
ACTIVE = ("R", "B")
READY = ("READY", "READY_WITH_WARNINGS")


def enabled():
    return getattr(settings, "AUTO_REVIEW_COORDINATOR_ENABLED", False)


def _kind(obj):
    return "problem" if isinstance(obj, Problem) else "contest"


def _runs(obj):
    model = ProblemReviewRun if isinstance(obj, Problem) else ContestReviewRun
    return model.objects.filter(**{_kind(obj): obj})


def remaining_credits(obj):
    return max(
        0,
        3
        - _runs(obj)
        .filter(
            workflow_version=WORKFLOW_VERSION,
            trigger_is_superuser=False,
            credit_consumed=True,
        )
        .count(),
    )


def invalidate_readiness(obj):
    """Update only the current-content overlay, never historical findings."""
    digest = current_content_hash(obj)
    evidence = (
        build_problem_manifest(obj, include_execution=True)
        if isinstance(obj, Problem)
        else None
    )
    fresh_ids, stale_ids = [], []
    runs = list(
        _runs(obj)
        .filter(workflow_version=WORKFLOW_VERSION)
        .only("id", "snapshot", "content_hash", "workflow_version")
    )
    if isinstance(obj, Contest):
        warm_contest_review_evidence(runs)
    for run in runs:
        (
            fresh_ids
            if not review_change_reasons(
                run, obj, content_hash=digest, snapshot=evidence
            )
            else stale_ids
        ).append(run.pk)
    _runs(obj).filter(pk__in=fresh_ids).update(current_stale=False)
    _runs(obj).filter(pk__in=stale_ids).update(current_stale=True)
    if isinstance(obj, Problem):
        contest_ids = list(
            ContestReviewDependency.objects.filter(problem=obj)
            .values_list("contest_run__contest_id", flat=True)
            .distinct()
        )
        memo = {obj.pk: digest}
        parent_hashes = {}
        # Contest is not a CacheableModel; load affected parents in one query.
        for contest in Contest.objects.filter(pk__in=contest_ids):
            parent_hashes[contest.pk] = current_content_hash(
                contest, problem_hashes=memo
            )
        fresh_ids, stale_ids = [], []
        parents = list(
            ContestReviewRun.objects.filter(
                contest_id__in=contest_ids, workflow_version=WORKFLOW_VERSION
            ).only("id", "contest_id", "snapshot", "content_hash", "workflow_version")
        )
        warm_contest_review_evidence(parents)
        for parent in parents:
            target = (
                fresh_ids
                if not review_change_reasons(
                    parent,
                    Contest(pk=parent.contest_id),
                    content_hash=parent_hashes[parent.contest_id],
                )
                else stale_ids
            )
            target.append(parent.pk)
        ContestReviewRun.objects.filter(pk__in=fresh_ids).update(current_stale=False)
        ContestReviewRun.objects.filter(pk__in=stale_ids).update(current_stale=True)


def warm_contest_review_evidence(runs):
    """Batch dependency freshness across historical runs; no per-run queries."""
    if not runs:
        return
    rows = list(
        ContestReviewDependency.objects.filter(
            contest_run_id__in=[run.pk for run in runs]
        ).values(
            "contest_run_id",
            "problem_id",
            "problem_run_id",
            "problem_run__current_stale",
            "content_hash",
            "problem_run__snapshot",
            "problem_run__content_hash",
        )
    )
    latest = dict(
        ProblemReviewRun.objects.filter(
            problem_id__in={row["problem_id"] for row in rows},
            workflow_version=WORKFLOW_VERSION,
            status="D",
        )
        .values("problem_id")
        .annotate(latest=Max("id"))
        .values_list("problem_id", "latest")
    )
    by_run = {}
    for row in rows:
        by_run.setdefault(row["contest_run_id"], []).append(row)
    for run in runs:
        run._review_dependency_rows = by_run.get(run.pk, [])
        run._latest_completed_problem_reviews = latest


def review_change_reasons(run, obj, *, content_hash=None, snapshot=None):
    """Current freshness overlay; never rewrite a saved verdict or enqueue AI."""
    if not run or run.workflow_version != WORKFLOW_VERSION:
        return []
    digest = content_hash if content_hash is not None else current_content_hash(obj)
    reasons = []
    if reviewed_content_hash(run) != digest:
        reasons.append(_("Reviewed content has changed."))
    if isinstance(obj, Problem):
        if "statement" in run.snapshot and "config" in run.snapshot:
            current = (
                snapshot
                if snapshot is not None
                else build_problem_manifest(obj, include_execution=True)
            )
            run._current_review_snapshot = current
            if reviewed_content_hash(run) == digest and execution_results_changed(
                run.snapshot, current
            ):
                reasons.append(_("Solution Code or validation results have changed."))
    else:
        dependencies = getattr(run, "_review_dependency_rows", None)
        if dependencies is None:
            dependencies = list(
                run.child_dependencies.values(
                    "problem_id", "problem_run_id", "problem_run__current_stale"
                )
            )
        latest = getattr(run, "_latest_completed_problem_reviews", None)
        if latest is None:
            latest = dict(
                ProblemReviewRun.objects.filter(
                    problem_id__in=[row["problem_id"] for row in dependencies],
                    workflow_version=WORKFLOW_VERSION,
                    status="D",
                )
                .values("problem_id")
                .annotate(latest=Max("id"))
                .values_list("problem_id", "latest")
            )
        if any(
            latest.get(row["problem_id"], 0) > (row["problem_run_id"] or 0)
            for row in dependencies
        ):
            reasons.append(
                _(
                    "This contest uses an older problem review. Run the contest review again to use the latest result."
                )
            )
        if any(row.get("problem_run__current_stale") for row in dependencies):
            reasons.append(
                _(
                    "An included problem's review is no longer current. Update its review before rerunning the contest review."
                )
            )
    return reasons


def review_content_change_labels(before, after):
    """Small field-level diff for readers, without source/JSON dumps."""
    if before.get("kind") == "contest":
        old, new = contest_review_content(before), contest_review_content(after)
        labels = {
            "config": _("Contest rules, duration or judging settings changed."),
            "items": _("Contest problems, ordering or scoring changed."),
            "children": _("An included problem's content changed."),
            "quiz_content": _("Included quiz content changed."),
        }
    else:
        old, new = problem_review_content(before), problem_review_content(after)
        labels = {
            "statement": _("Statement changed."),
            "config": _("Resource limits or scoring settings changed."),
            "data_config": _("Test generation or judging configuration changed."),
            "tests": _("Official testcase configuration changed."),
            "artifacts": _(
                "Test data, judging source files or the PDF statement changed."
            ),
            "solutions": _(
                "Solution Code source, language or expected verdict changed."
            ),
            "editorial": _("Editorial changed."),
            "language_limits": _("Language-specific limits changed."),
            "allowed_languages": _("Allowed languages changed."),
            "language_templates": _("Contestant starter code changed."),
            "translations": _("Translated statements changed."),
        }
    return [message for key, message in labels.items() if old.get(key) != new.get(key)]


def review_metadata_changes(run, obj):
    """Explain nontechnical changes without converting them into stale results."""
    if not run:
        return []
    labels = {
        "authors": _("Authors changed; publication access checks are separate."),
        "curators": _("Curators changed; publication access checks are separate."),
        "testers": _("Testers changed; publication access checks are separate."),
    }
    old_roles = run.snapshot.get("roles", {})
    changes = []
    for role, message in labels.items():
        if role not in old_roles:
            continue
        method = getattr(obj, "get_" + role[:-1] + "_ids", None)
        current_ids = method() if method else getattr(obj, "_" + role[:-1] + "_ids")()
        if role in old_roles and sorted(old_roles[role]) != sorted(current_ids):
            changes.append(message)
    return changes


def is_review_readable(obj, user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile_id = user.profile.id
    # Object-local Lock must not revoke existing team access to review history.
    return profile_id in obj.editor_ids or user.has_perm("judge.edit_all_" + _kind(obj))


def _authorize(obj, actor):
    user = actor.user
    if not user.is_superuser and (
        not user.has_perm("judge.add_problem")
        or not (
            actor.pk in obj.editor_ids
            or obj.is_editable_by(user)
            or user.has_perm("judge.edit_all_" + _kind(obj))
        )
    ):
        raise PermissionDenied(_("You do not have permission to run this review."))


def enqueue(kind, run_id, countdown=0):
    transaction.on_commit(lambda: dispatch_advance(kind, run_id, countdown))


def _create(obj, actor, snapshot):
    model = ProblemReviewRun if isinstance(obj, Problem) else ContestReviewRun
    run = model.objects.create(
        **{_kind(obj): obj},
        triggered_by=actor,
        workflow_version=WORKFLOW_VERSION,
        snapshot=snapshot,
        content_hash=snapshot["content_hash"],
        input_hash=snapshot["input_hash"],
        trigger_is_superuser=actor.user.is_superuser,
    )
    _runs(obj).filter(superseded_by__isnull=True).exclude(pk=run.pk).update(
        superseded_by=run
    )
    enqueue(_kind(obj), run.pk)
    return run


def _admit(obj, actor, snapshot, confirm_unchanged):
    _authorize(obj, actor)
    if not actor.user.is_superuser:
        if _runs(obj).filter(status__in=ACTIVE).exists():
            raise ValidationError(
                _(
                    "A review is already running or awaiting recovery. Retry its failed section or cancel it first."
                )
            )
        if remaining_credits(obj) == 0:
            raise ValidationError(
                _("No author review credits remain. Please ask a superuser.")
            )
    previous = _runs(obj).order_by("-id").first()
    if (
        previous
        and (
            problem_review_input_hash(previous.snapshot)
            if isinstance(obj, Problem) and "statement" in previous.snapshot
            else previous.input_hash
        )
        == snapshot["input_hash"]
        and not confirm_unchanged
    ):
        raise ValidationError(
            _(
                "The reviewed inputs have not changed. Confirm a fresh review to continue."
            ),
            code="unchanged",
        )
    return _create(obj, actor, snapshot)


def _prepared_problem_snapshot(obj):
    try:
        return build_problem_snapshot(obj)
    except SnapshotError as error:
        raise ValidationError(str(error), code="preparation") from error
    except OSError as error:
        raise ValidationError(
            _(
                "A required package artifact could not be read. Fix the package before reviewing."
            ),
            code="preparation",
        ) from error


@transaction.atomic
def start_problem_review(
    problem, actor, *, confirm_unchanged=False, expected_content_hash=None
):
    if not enabled():
        raise ValidationError(_("The coordinator review is currently disabled."))
    obj = Problem.objects.select_for_update().get(pk=problem.pk)
    _authorize(obj, actor)
    data_config = ProblemData.objects.filter(problem=obj).values().first()
    if not is_supported_problem(data_config):
        raise ValidationError(
            _(
                "This problem type is not supported by coordinator review yet; ask a superuser for manual review."
            ),
            code="unsupported",
        )
    snapshot = _prepared_problem_snapshot(obj)
    if (
        expected_content_hash is not None
        and snapshot["content_hash"] != expected_content_hash
    ):
        raise ValidationError(
            _(
                "The content changed since you opened the confirmation. Please check the updated version and confirm again."
            )
        )
    errors = preparation_errors(snapshot)
    if errors:
        raise ValidationError(
            [ValidationError(message, code="preparation") for message in errors]
        )
    return _admit(obj, actor, snapshot, confirm_unchanged)


@transaction.atomic
def start_contest_review(
    contest,
    actor,
    *,
    selected_problem_ids=(),
    confirm_unchanged=False,
    expected_content_hash=None
):
    if not enabled():
        raise ValidationError(_("The coordinator review is currently disabled."))
    obj = Contest.objects.select_for_update().get(pk=contest.pk)
    _authorize(obj, actor)
    if (
        expected_content_hash is not None
        and current_content_hash(obj) != expected_content_hash
    ):
        raise ValidationError(
            _(
                "The content changed since you opened the confirmation. Please check the updated version and confirm again."
            )
        )
    if not actor.user.is_superuser and (
        _runs(obj).filter(status__in=ACTIVE).exists() or remaining_credits(obj) == 0
    ):
        raise ValidationError(
            _("The contest has no available review slot or author credits.")
        )
    item_ids = list(
        obj.contest_problems.exclude(problem_id=None).values_list(
            "problem_id", flat=True
        )
    )
    try:
        selected = {int(value) for value in selected_problem_ids}
    except (ValueError, TypeError):
        raise ValidationError(_("Invalid selected problem."))
    if not selected.issubset(set(item_ids)):
        raise ValidationError(_("The selected problem is not in this contest."))
    # Stable parent-then-child lock order, including all shared child identities.
    problems = list(
        Problem.objects.select_for_update().filter(pk__in=item_ids).order_by("pk")
    )
    errors = preparation_errors(build_contest_snapshot(obj, []))
    if errors:
        raise ValidationError(
            [ValidationError(message, code="preparation") for message in errors]
        )
    contexts = []
    data_configs = {
        row["problem_id"]: row
        for row in ProblemData.objects.filter(problem_id__in=item_ids).values()
    }
    latest_by_problem = {}
    active_by_identity = {}
    for child_run in ProblemReviewRun.objects.filter(
        problem_id__in=item_ids, workflow_version=WORKFLOW_VERSION
    ).order_by("-id"):
        latest_by_problem.setdefault(child_run.problem_id, child_run)
        if child_run.status in ACTIVE:
            active_by_identity.setdefault(
                (child_run.problem_id, reviewed_content_hash(child_run)), child_run
            )
    for problem in problems:
        content_hash = current_content_hash(problem)
        latest = latest_by_problem.get(problem.pk)
        owned = False
        supported = is_supported_problem(data_configs.get(problem.pk))
        if problem.pk in selected and supported:
            _authorize(problem, actor)
            active = active_by_identity.get((problem.pk, content_hash))
            if active:
                latest = active
            else:
                snapshot = _prepared_problem_snapshot(problem)
                errors = preparation_errors(snapshot)
                if errors:
                    raise ValidationError(
                        [
                            ValidationError(message, code="preparation")
                            for message in errors
                        ]
                    )
                latest = _admit(problem, actor, snapshot, True)
                owned = True
        contexts.append(
            {
                "problem_id": problem.pk,
                "name": problem.name,
                "code": problem.code,
                "content_hash": content_hash,
                "review_run_id": latest.pk if latest else None,
                "owned": owned,
                "selected": problem.pk in selected,
                "unsupported": not supported,
            }
        )
    snapshot = build_contest_snapshot(obj, contexts)
    run = _admit(obj, actor, snapshot, confirm_unchanged)
    ContestReviewDependency.objects.bulk_create(
        [
            ContestReviewDependency(
                contest_run=run,
                problem_id=context["problem_id"],
                problem_run_id=context["review_run_id"],
                content_hash=context["content_hash"],
                owned=context["owned"],
                selected=context["selected"],
            )
            for context in contexts
        ]
    )
    run.problem_review_runs.add(
        *(context["review_run_id"] for context in contexts if context["review_run_id"])
    )
    return run


def publication_eligibility(run, obj):
    return not publication_blockers(run, obj)


def publication_blockers(run, obj, *, current_snapshot=None):
    """Current publication checks, separate from the immutable review verdict.

    Return reader-facing reasons, never raw evidence IDs or provider diagnostics.
    A presentation caller may reuse the current snapshot it just read in this
    request. Publication mutations omit it and independently recapture the data.
    This is also the policy behind publication_eligibility, so UI explanations
    cannot silently diverge from admission/approval checks.
    """
    if (
        not run
        or run.workflow_version != WORKFLOW_VERSION
        or run.status != run.DONE
        or run.readiness not in READY
    ):
        return [
            _("Complete a review with a Ready or Ready with warnings result first.")
        ]
    if getattr(run, _kind(obj) + "_id") != obj.pk:
        return [_("This review belongs to different content.")]
    current_hash = (
        current_snapshot["content_hash"]
        if isinstance(obj, Problem) and current_snapshot is not None
        else current_content_hash(obj)
    )
    if reviewed_content_hash(run) != current_hash:
        return [
            _(
                "The reviewed content has changed. Run a new review before requesting publication."
            )
        ]
    blockers = []
    if isinstance(obj, Contest):
        blockers.extend(
            review_change_reasons(run, obj, content_hash=reviewed_content_hash(run))
        )
    exposure_reason = _(
        "Possible pre-release access requires administrator review. This is a risk signal, not proof of a leak."
    )
    if publication_exposure_concern(obj):
        blockers.append(exposure_reason)
    stages = list(run.coordinator_stages.all())
    if {stage.stage_id for stage in stages} != set(
        stage_definitions(_kind(obj))
    ) or any(stage.status != ReviewStage.DONE for stage in stages):
        blockers.append(_("Some required review checks are missing or unfinished."))
    if isinstance(obj, Contest):
        dependencies = list(
            run.child_dependencies.select_related("problem_run").prefetch_related(
                "problem_run__coordinator_stages"
            )
        )
        problems = {
            p.pk: p
            for p in Problem.get_cached_instances(
                *(dep.problem_id for dep in dependencies)
            )
        }
        if not dependencies:
            blockers.append(
                _("No eligible problem reviews are attached to this contest review.")
            )
        for dep in dependencies:
            problem = problems[dep.problem_id]
            reasons = publication_blockers(dep.problem_run, problem)
            dependency_hash = normalized_dependency_hash(
                dep.content_hash, dep.problem_run
            )
            if dependency_hash != current_content_hash(problem) or (
                dep.problem_run
                and reviewed_content_hash(dep.problem_run) != dependency_hash
            ):
                reasons.append(
                    _(
                        "The attached problem review no longer matches the current problem."
                    )
                )
            blockers.extend(
                _("Problem %(code)s: %(reason)s")
                % {"code": problem.code, "reason": reason}
                for reason in dict.fromkeys(reasons)
            )
        return blockers
    try:
        snapshot = (
            current_snapshot
            if current_snapshot is not None
            else build_problem_snapshot(obj, include_statement_sources=False)
        )
    except (SnapshotError, OSError):
        return blockers + [
            _(
                "The current problem package could not be checked. Ask an administrator to investigate."
            )
        ]
    if "statement" in run.snapshot and "config" in run.snapshot:
        # Reuse the exact certified execution binding, while independently checking
        # current technical content and current evidence. Administrative edits must
        # not invalidate a historical attestation's old package-hash encoding.
        evidence_valid = not preparation_errors(
            run.snapshot
        ) and not execution_results_changed(run.snapshot, snapshot)
    else:
        evidence_valid = (
            not preparation_errors(snapshot)
            and run.input_hash == snapshot["input_hash"]
        )
    if not evidence_valid:
        blockers.append(
            _(
                "The current package or execution evidence does not match the reviewed evidence. Check Run Validation and Solution Codes before requesting publication."
            )
        )
    return blockers


def settle(run, *, cancelled=False, preflight=False):
    """Caller holds the run lock; charges and provider usage are independent."""
    if run.credit_settled:
        return
    unresolved_infra = (
        run.status == run.BLOCKED
        or run.coordinator_stages.exclude(error_category="")
        .exclude(status=ReviewStage.DONE)
        .exists()
    )
    if isinstance(run, ContestReviewRun):
        child_ids = run.child_dependencies.exclude(problem_run=None).values_list(
            "problem_run_id", flat=True
        )
        unresolved_infra |= (
            ReviewStage.objects.filter(problem_run_id__in=child_ids)
            .exclude(error_category="")
            .exclude(status=ReviewStage.DONE)
            .exists()
        )
    run.credit_consumed = not (
        run.trigger_is_superuser or preflight or unresolved_infra
    )
    run.credit_settled = True
    run.save(update_fields=["credit_consumed", "credit_settled"])


@transaction.atomic
def cancel_review(run, actor, *, _owned_cascade=False):
    obj = run.problem if isinstance(run, ProblemReviewRun) else run.contest
    type(obj).objects.select_for_update().get(pk=obj.pk)
    if not _owned_cascade:
        _authorize(obj, actor)
    current = type(run).objects.select_for_update().get(pk=run.pk)
    if current.status not in ACTIVE:
        raise ValidationError(
            _("Only a running or infrastructure-blocked review can be cancelled.")
        )
    if isinstance(current, ContestReviewRun):
        dependencies = list(
            current.child_dependencies.filter(owned=True)
            .select_related("problem_run")
            .order_by("problem_id")
        )
        for dep in dependencies:
            if dep.problem_run and dep.problem_run.status in ACTIVE:
                # The already-authorized parent owns this child lifecycle. A
                # later role change on the child must not prevent cancellation
                # of the parent's own unfinished work.
                cancel_review(dep.problem_run, actor, _owned_cascade=True)
    settle(current, cancelled=True)
    current.status = current.CANCELLED
    current.finished_at = timezone.now()
    current.save(update_fields=["status", "finished_at"])
    current.coordinator_stages.filter(status=ReviewStage.RUNNING).update(
        lease_token=None, lease_expires_at=None
    )
    if isinstance(current, ProblemReviewRun):
        cancelled_jobs = list(
            ReviewEvidenceJob.objects.filter(
                problem_run=current, status__in=("P", "R")
            ).values_list("job_id", flat=True)
        )
        ReviewEvidenceJob.objects.filter(
            problem_run=current, status__in=("P", "R")
        ).update(status="C", error="Review cancelled", finished=timezone.now())
        transaction.on_commit(
            lambda: cancel_evidence_jobs([str(identity) for identity in cancelled_jobs])
        )
    return current


@transaction.atomic
def retry_stage(run, stage_id, actor):
    obj = run.problem if isinstance(run, ProblemReviewRun) else run.contest
    _authorize(obj, actor)
    current = type(run).objects.select_for_update().get(pk=run.pk)
    if current.status != current.BLOCKED:
        raise ValidationError(
            _("Retry is available only for an infrastructure-blocked review.")
        )
    stage = (
        current.coordinator_stages.select_for_update().filter(stage_id=stage_id).first()
    )
    if stage is None:
        raise ValidationError(_("Unknown review section."))
    if stage.status != ReviewStage.ERROR:
        raise ValidationError(_("This section has no infrastructure failure to retry."))
    if stage.retry_at and stage.retry_at > timezone.now():
        raise ValidationError(
            _(
                "The AI service requested a cooldown. Please wait before retrying this section."
            )
        )
    if not actor.user.is_superuser and stage.manual_retries >= 1:
        raise PermissionDenied(
            _("The author retry allowance is exhausted. Please ask a superuser.")
        )
    stage.manual_retries += 1
    stage.status = ReviewStage.PENDING
    stage.reason = ""
    stage.lease_token = None
    stage.lease_expires_at = None
    stage.save(
        update_fields=[
            "manual_retries",
            "status",
            "reason",
            "lease_token",
            "lease_expires_at",
        ]
    )
    current.status = current.RUNNING
    invalidate_review_sections(
        current, {stage_id}, stage_definitions(_kind(obj)), include_roots=False
    )
    current.summary_report = ""
    current.readiness = "INCOMPLETE"
    current.finished_at = None
    current.save(update_fields=["status", "summary_report", "readiness", "finished_at"])
    enqueue(_kind(obj), run.pk)
    return stage


def invalidate_review_sections(run, roots, definitions, include_roots=True):
    """Reassess affected consumers, retaining attempt history and unrelated work.

    Caller holds the run lock. Clearing leases prevents in-flight old contexts
    from committing results after recovery. Independently errored consumers keep
    their error/retry allowance, but their next retry gets refreshed evidence.
    """
    affected = set(roots)
    while True:
        expanded = affected | {
            name
            for name, definition in definitions.items()
            if affected.intersection(definition.get("dependencies", []))
        }
        if expanded == affected:
            break
        affected = expanded
    if not include_roots:
        affected -= set(roots)
    rows = list(
        run.coordinator_stages.select_for_update().filter(stage_id__in=affected)
    )
    for stage in rows:
        if stage.status != ReviewStage.ERROR:
            stage.status = ReviewStage.PENDING
            stage.error_category = ""
            stage.reason = ""
            stage.retry_at = None
        stage.input_json = {}
        stage.request_json = {}
        stage.output_json = {}
        stage.lease_token = None
        stage.lease_expires_at = None
        stage.finished_at = None
    if rows:
        ReviewStage.objects.bulk_update(
            rows,
            [
                "status",
                "error_category",
                "reason",
                "retry_at",
                "input_json",
                "request_json",
                "output_json",
                "lease_token",
                "lease_expires_at",
                "finished_at",
            ],
        )
