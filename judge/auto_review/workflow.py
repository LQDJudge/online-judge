"""Review admission, current eligibility, cancellation and section recovery."""

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
    ProblemReviewRun,
    ReviewStage,
)
from judge.auto_review.phases import contest_child_readiness, stage_definitions
from judge.auto_review.contest_exposure import publication_exposure_concern
from judge.auto_review.problem_context import build_problem_context
from judge.auto_review.contest_context import build_contest_context
from judge.auto_review.review_context import current_content_hash
from judge.auto_review.preparation import preparation_errors
from judge.auto_review.review_hash import reviewed_content_hash as reviewed_content_hash
from judge.auto_review.context_utils import ReviewInputError
from judge.auto_review.scheduling import definitions_for_run, schedule_phases, settle
from judge.auto_review.test_statistics import cancel_extractions, extraction_request_id
from judge.utils.public_visibility import is_site_public

ACTIVE = ("R", "B")
READY = ("READY", "READY_WITH_WARNINGS")


def _kind(obj):
    return "problem" if isinstance(obj, Problem) else "contest"


def _runs(obj):
    model = ProblemReviewRun if isinstance(obj, Problem) else ContestReviewRun
    return model.objects.filter(**{_kind(obj): obj})


def remaining_credits(obj):
    return max(
        0,
        3 - _runs(obj).filter(trigger_is_superuser=False, credit_consumed=True).count(),
    )


def is_review_readable(obj, user):
    return user.is_authenticated and (
        user.is_superuser
        or user.profile.pk in obj.editor_ids
        or user.has_perm("judge.edit_all_" + _kind(obj))
    )


def contest_report_readable(run, user):
    """A contest report may contain evidence from every linked problem report."""
    if user.is_superuser:
        return True
    ids = list(run.child_dependencies.values_list("problem_id", flat=True))
    problems = Problem.get_cached_instances(*ids)
    return len(problems) == len(set(ids)) and all(
        is_review_readable(problem, user) for problem in problems
    )


def _authorize(obj, actor):
    user = actor.user
    if not is_review_readable(obj, user) or (
        not user.is_superuser and not user.has_perm("judge.add_problem")
    ):
        raise PermissionDenied(_("You do not have permission to run this review."))


def enqueue(kind, run_id, countdown=0, completed_phase=None):
    # A normal Python function, not another queued coordinator task.
    transaction.on_commit(lambda: schedule_phases(kind, run_id, completed_phase))


def review_is_current(run, obj):
    try:
        return bool(run.input_hash) and run.input_hash == current_content_hash(obj)
    except (ReviewInputError, OSError, ValueError):
        return False


def review_change_reasons(run, obj, *, content_hash=None):
    if not run:
        return []
    reasons = []
    current = (
        bool(run.input_hash) and run.input_hash == content_hash
        if content_hash is not None
        else review_is_current(run, obj)
    )
    if not current:
        reasons.append(
            _("Reviewed content has changed.")
            if run.input_hash
            else _("Run a new review before requesting publication.")
        )
    if isinstance(obj, Contest):
        dependencies = getattr(run, "_review_dependency_rows", None)
        if dependencies is None:
            dependencies = list(
                run.child_dependencies.values("problem_id", "problem_run_id")
            )
        latest = getattr(run, "_latest_completed_problem_reviews", None)
        if latest is None:
            latest = dict(
                ProblemReviewRun.objects.filter(
                    problem_id__in=[d["problem_id"] for d in dependencies], status="D"
                )
                .values("problem_id")
                .annotate(latest=Max("id"))
                .values_list("problem_id", "latest")
            )
        if any(
            latest.get(d["problem_id"], 0) > (d["problem_run_id"] or 0)
            for d in dependencies
        ):
            reasons.append(
                _(
                    "This contest uses an older problem review. Run the contest review again to use the latest result."
                )
            )
    return reasons


def warm_contest_review_evidence(runs):
    rows = list(
        ContestReviewDependency.objects.filter(
            contest_run_id__in=[r.pk for r in runs]
        ).values("contest_run_id", "problem_id", "problem_run_id")
    )
    latest = dict(
        ProblemReviewRun.objects.filter(
            problem_id__in={r["problem_id"] for r in rows}, status="D"
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


def invalidate_readiness(obj):
    runs = list(_runs(obj).filter(status="D").prefetch_related("coordinator_stages"))
    try:
        current_hash = current_content_hash(obj) if runs else None
    except (ReviewInputError, OSError, ValueError):
        # Unreadable inputs cannot establish freshness. Do not fail a content
        # save or a manual publication after its transaction has committed.
        current_hash = ""
    if isinstance(obj, Contest):
        warm_contest_review_evidence(runs)
    changed = []
    for run in runs:
        stale = bool(review_change_reasons(run, obj, content_hash=current_hash))
        if run.current_stale != stale:
            run.current_stale = stale
            changed.append(run)
    if changed:
        _runs(obj).model.objects.bulk_update(changed, ["current_stale"])
    if isinstance(obj, Problem):
        ids = (
            ContestReviewDependency.objects.filter(problem=obj)
            .values_list("contest_run__contest_id", flat=True)
            .distinct()
        )
        for contest in Contest.objects.filter(pk__in=ids):
            invalidate_readiness(contest)


def _check_admission(obj, actor, identity, confirm_unchanged):
    _authorize(obj, actor)
    if is_site_public(obj):
        raise ValidationError(
            _(
                "This content is already public on the site. Review is for content awaiting publication."
            ),
            code="public",
        )
    if _runs(obj).filter(status__in=ACTIVE).exists():
        raise ValidationError(
            _(
                "A review is already running. Cancel it or recover its failed section first."
            )
        )
    if not actor.user.is_superuser and remaining_credits(obj) == 0:
        raise ValidationError(
            _("No author review credits remain. Please ask a superuser.")
        )
    previous = _runs(obj).order_by("-id").first()
    if previous and previous.input_hash == identity and not confirm_unchanged:
        raise ValidationError(
            _(
                "The reviewed inputs have not changed. Confirm a fresh review to continue."
            ),
            code="unchanged",
        )


def _create(obj, actor, context):
    model = ProblemReviewRun if isinstance(obj, Problem) else ContestReviewRun
    run = model.objects.create(
        **{_kind(obj): obj},
        triggered_by=actor,
        input_hash=context["input_hash"],
        trigger_is_superuser=actor.user.is_superuser
    )
    _runs(obj).filter(superseded_by__isnull=True).exclude(pk=run.pk).update(
        superseded_by=run
    )
    ReviewStage.objects.bulk_create(
        [
            ReviewStage(**{_kind(obj) + "_run": run}, stage_id=key)
            for key in stage_definitions(_kind(obj), context)
        ]
    )
    enqueue(_kind(obj), run.pk)
    return run


def _prepared_problem_context(obj):
    if is_site_public(obj):
        raise ValidationError(
            _("This problem is already public on the site."), code="public"
        )
    try:
        context = build_problem_context(obj)
    except (ReviewInputError, OSError) as exc:
        raise ValidationError(
            _(
                "A required problem file could not be read. Check Test Data before reviewing."
            ),
            code="preparation",
        ) from exc
    errors = preparation_errors(context)
    if errors:
        raise ValidationError(
            [ValidationError(message, code="preparation") for message in errors]
        )
    return context


@transaction.atomic
def start_problem_review(
    problem, actor, *, confirm_unchanged=False, expected_content_hash=None
):
    obj = Problem.objects.select_for_update().get(pk=problem.pk)
    _authorize(obj, actor)
    context = _prepared_problem_context(obj)
    if (
        expected_content_hash is not None
        and context["input_hash"] != expected_content_hash
    ):
        raise ValidationError(_("The content changed. Check it and confirm again."))
    _check_admission(obj, actor, context["input_hash"], confirm_unchanged)
    return _create(obj, actor, context)


@transaction.atomic
def start_contest_review(
    contest,
    actor,
    *,
    selected_problem_ids=(),
    confirm_unchanged=False,
    expected_content_hash=None
):
    obj = Contest.objects.select_for_update().get(pk=contest.pk)
    _authorize(obj, actor)
    identity = current_content_hash(obj)
    if expected_content_hash is not None and identity != expected_content_hash:
        raise ValidationError(_("The content changed. Check it and confirm again."))
    _check_admission(obj, actor, identity, confirm_unchanged)
    item_ids = set(
        obj.contest_problems.exclude(problem_id=None).values_list(
            "problem_id", flat=True
        )
    )
    try:
        selected = {int(value) for value in selected_problem_ids}
    except (TypeError, ValueError) as exc:
        raise ValidationError(_("Invalid selected problem.")) from exc
    if not selected.issubset(item_ids):
        raise ValidationError(_("The selected problem is not in this contest."))
    problems = list(
        Problem.objects.select_for_update().filter(pk__in=item_ids).order_by("pk")
    )
    public = [p.code for p in problems if is_site_public(p)]
    if public:
        raise ValidationError(
            _("This contest contains already-public problems: %(codes)s.")
            % {"codes": ", ".join(public)}
        )
    contexts, prepared = [], {}
    for problem in problems:
        if problem.pk in selected:
            _authorize(problem, actor)
            child_context = _prepared_problem_context(problem)
            _check_admission(problem, actor, child_context["input_hash"], True)
            prepared[problem.pk] = child_context
        elif not is_review_readable(problem, actor.user):
            raise PermissionDenied(
                _("You do not have permission to view this problem review.")
            )
    # All preparation is checked before any child run or on-commit delivery exists.
    for problem in problems:
        owned = problem.pk in prepared
        child = (
            _create(problem, actor, prepared[problem.pk])
            if owned
            else ProblemReviewRun.objects.filter(problem=problem, status="D")
            .order_by("-id")
            .first()
        )
        if not owned and (not child or not review_is_current(child, problem)):
            raise ValidationError(
                _(
                    "Problem %(code)s needs a current review. Select it to run a new one."
                )
                % {"code": problem.code}
            )
        contexts.append(
            {
                "problem_id": problem.pk,
                "review_run_id": child.pk if child else None,
                "content_hash": current_content_hash(problem),
                "owned": owned,
            }
        )
    context = build_contest_context(obj, contexts)
    errors = preparation_errors(context)
    if errors:
        raise ValidationError(errors)
    run = _create(obj, actor, context)
    ContestReviewDependency.objects.bulk_create(
        [
            ContestReviewDependency(
                contest_run=run,
                problem_id=c["problem_id"],
                problem_run_id=c["review_run_id"],
                content_hash=c["content_hash"],
                owned=c["owned"],
            )
            for c in contexts
        ]
    )
    return run


def publication_eligibility(run, obj):
    return not publication_blockers(run, obj)


def publication_blockers(run, obj, *, current_context=None):
    readiness = getattr(run, "readiness", "")
    if not run or run.status != "D" or readiness not in READY:
        return [
            _("Complete a review with a Ready or Ready with warnings result first.")
        ]
    if getattr(run, _kind(obj) + "_id") != obj.pk:
        return [_("This review belongs to different content.")]
    reasons = review_change_reasons(
        run,
        obj,
        content_hash=(
            current_context.get("input_hash")
            if isinstance(obj, Problem) and current_context
            else None
        ),
    )
    if _runs(obj).filter(status__in=ACTIVE).exists():
        reasons.append(_("Wait for the running review to finish."))
    if isinstance(obj, Contest) and contest_child_readiness(run):
        reasons.append(
            _("An included problem review is missing, incomplete, or needs attention.")
        )
    if publication_exposure_concern(obj):
        reasons.append(
            _(
                "Possible pre-release access requires administrator review. This is a risk signal, not proof of a leak."
            )
        )
    stages = list(run.coordinator_stages.all())
    definitions = definitions_for_run(run)
    by_id = {s.stage_id: s for s in stages}
    if any(
        key not in by_id or by_id[key].status != ReviewStage.DONE for key in definitions
    ):
        reasons.append(_("Some required review checks are missing or unfinished."))
    return list(dict.fromkeys(reasons))


@transaction.atomic
def cancel_review(run, actor, *, _owned_cascade=False):
    obj = run.problem if hasattr(run, "problem_id") else run.contest
    if not _owned_cascade:
        _authorize(obj, actor if hasattr(actor, "user") else actor.profile)
    current = type(run).objects.select_for_update().get(pk=run.pk)
    if current.status not in ACTIVE:
        return current
    settle(current, cancelled=True)
    current.status, current.finished_at = current.CANCELLED, timezone.now()
    current.save(update_fields=["status", "finished_at"])
    extraction_ids = [
        extraction_request_id(pk, token)
        for pk, token in current.coordinator_stages.filter(
            stage_id="extract_stats", status=ReviewStage.RUNNING
        )
        .exclude(lease_token=None)
        .values_list("pk", "lease_token")
    ]
    current.coordinator_stages.exclude(status=ReviewStage.DONE).update(lease_token=None)
    if extraction_ids:
        transaction.on_commit(lambda: cancel_extractions(extraction_ids))
    if hasattr(current, "contest_id"):
        for dep in current.child_dependencies.filter(owned=True).select_related(
            "problem_run"
        ):
            if dep.problem_run and dep.problem_run.status in ACTIVE:
                cancel_review(dep.problem_run, actor, _owned_cascade=True)
    else:
        for parent in ContestReviewRun.objects.filter(
            child_dependencies__problem_run=current, status__in=ACTIVE
        ).distinct():
            cancel_review(parent, actor, _owned_cascade=True)
    return current


def cancel_for_save(obj, actor):
    # View/admin authorization already established; Lock is rechecked by mutation boundary.
    with transaction.atomic():
        for run in _runs(obj).filter(status__in=ACTIVE).order_by("pk"):
            cancel_review(run, actor, _owned_cascade=True)
        if isinstance(obj, Problem):
            for parent in (
                ContestReviewRun.objects.filter(
                    child_dependencies__problem=obj, status__in=ACTIVE
                )
                .distinct()
                .order_by("pk")
            ):
                cancel_review(parent, actor, _owned_cascade=True)
    transaction.on_commit(lambda: invalidate_readiness(obj))


@transaction.atomic
def retry_stage(run, stage_id, actor):
    obj = run.problem if hasattr(run, "problem_id") else run.contest
    _authorize(obj, actor)
    current = type(run).objects.select_for_update().get(pk=run.pk)
    if current.status not in ACTIVE or not review_is_current(current, obj):
        raise ValidationError(
            _("This review cannot be retried. Start a current review instead.")
        )
    stage = (
        current.coordinator_stages.select_for_update()
        .filter(stage_id=stage_id, status=ReviewStage.ERROR)
        .first()
    )
    if stage is None:
        raise ValidationError(_("This section has no infrastructure failure to retry."))
    if stage.retry_at and stage.retry_at > timezone.now():
        raise ValidationError(
            _("Please wait for the service cooldown before retrying.")
        )
    if not actor.user.is_superuser and stage.manual_retries >= 1:
        raise PermissionDenied(
            _("The author retry allowance is exhausted. Please ask a superuser.")
        )
    stage.manual_retries += 1
    stage.status, stage.lease_token, stage.retry_at = ReviewStage.PENDING, None, None
    stage.save(update_fields=["manual_retries", "status", "lease_token", "retry_at"])
    current.status = current.RUNNING
    current.save(update_fields=["status"])
    enqueue(_kind(obj), current.pk, completed_phase="__retry__" + stage_id)
    return stage
