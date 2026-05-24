from dataclasses import asdict, dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from celery.result import AsyncResult

from judge.ml.semantic_search import (
    SEMANTIC_TABLE,
    SemanticSearchUnavailable,
    _semantic_vector_search,
    _stored_problem_embedding,
    get_semantic_dims,
    get_semantic_model,
)
from judge.models import (
    Problem,
    ProblemDuplicateCandidate,
    ProblemDuplicateMergeHistory,
    ProblemDuplicateReport,
    ProblemDuplicateReviewHistory,
    Submission,
)

DEFAULT_DUPLICATE_SCORE = 0.97
DEFAULT_DUPLICATE_LIMIT = 100
DEFAULT_DUPLICATE_NEIGHBORS = 10
DUPLICATE_REPORT_REFRESH_TIMEOUT_SECONDS = 3600
DUPLICATE_MERGE_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class DuplicateProblemReportOptions:
    min_score: float = DEFAULT_DUPLICATE_SCORE
    limit: int = DEFAULT_DUPLICATE_LIMIT
    neighbors: int = DEFAULT_DUPLICATE_NEIGHBORS

    def as_dict(self):
        return asdict(self)


class DuplicateProblemReportRefreshPending(Exception):
    pass


class DuplicateProblemMergePending(Exception):
    pass


def get_latest_duplicate_problem_report(options=None):
    options = options or DuplicateProblemReportOptions()
    return (
        ProblemDuplicateReport.objects.filter(
            min_score=options.min_score,
            limit=options.limit,
            neighbors=options.neighbors,
            status=ProblemDuplicateReport.SUCCESS,
        )
        .order_by("-finished_at", "-started_at")
        .first()
    )


def get_cached_duplicate_problem_candidates(options=None):
    report = get_latest_duplicate_problem_report(options)
    if report is None:
        return None
    hidden_merge_pairs = _active_or_completed_merge_pairs()
    return [
        format_candidate(candidate)
        for candidate in report.candidates.filter(status=ProblemDuplicateCandidate.OPEN)
        if (candidate.source_code, candidate.target_code) not in hidden_merge_pairs
    ]


def get_or_compute_duplicate_problem_candidates(force=False, options=None, report=None):
    if not getattr(settings, "USE_ML", False):
        raise SemanticSearchUnavailable("USE_ML is disabled")

    options = options or DuplicateProblemReportOptions()
    if not force:
        candidates = get_cached_duplicate_problem_candidates(options)
        if candidates is not None:
            return candidates

    candidates = _generate_duplicate_problem_candidates(options)
    if report is not None:
        store_duplicate_problem_report_candidates(report, candidates)
    return candidates


# Backward-compatible name for callers/tests that still expect the old helper.
def get_duplicate_problem_candidates(force=False, options=None):
    return get_or_compute_duplicate_problem_candidates(force=force, options=options)


def schedule_duplicate_problem_report_refresh(options, requested_by=None):
    if is_duplicate_problem_report_refresh_pending():
        raise DuplicateProblemReportRefreshPending(
            "duplicate report refresh is pending"
        )

    from judge.tasks.semantic_search import refresh_duplicate_problem_report

    report = ProblemDuplicateReport.objects.create(
        min_score=options.min_score,
        limit=options.limit,
        neighbors=options.neighbors,
        status=ProblemDuplicateReport.PENDING,
        requested_by=requested_by,
    )
    task = refresh_duplicate_problem_report.delay(report.id)
    report.task_id = task.id
    report.save(update_fields=["task_id"])
    return task


def get_duplicate_problem_report_refresh_state():
    report = ProblemDuplicateReport.objects.order_by("-started_at").first()
    if report is None:
        return {"status": "IDLE"}
    if report.status == ProblemDuplicateReport.PENDING:
        report = _sync_pending_report_state(report)
    return format_report_state(report)


def mark_duplicate_problem_report_refresh_finished(status, report_id, **extra):
    report = ProblemDuplicateReport.objects.get(id=report_id)
    if status == "SUCCESS":
        report.status = ProblemDuplicateReport.SUCCESS
        report.candidate_count = int(extra.get("count", 0))
        report.error = ""
    else:
        report.status = ProblemDuplicateReport.FAILED
        report.error = extra.get("error", "")
    report.finished_at = timezone.now()
    report.save(update_fields=["status", "candidate_count", "error", "finished_at"])
    return report


def is_duplicate_problem_report_refresh_pending():
    get_duplicate_problem_report_refresh_state()
    return ProblemDuplicateReport.objects.filter(
        status=ProblemDuplicateReport.PENDING
    ).exists()


def _active_or_completed_merge_pairs():
    sync_pending_duplicate_problem_merges()
    return set(
        ProblemDuplicateMergeHistory.objects.filter(
            status__in=[
                ProblemDuplicateMergeHistory.PENDING,
                ProblemDuplicateMergeHistory.RUNNING,
                ProblemDuplicateMergeHistory.SUCCESS,
            ]
        ).values_list("source_code", "target_code")
    )


def _sync_pending_report_state(report):
    if report.task_id:
        result = AsyncResult(report.task_id)
        if result.ready():
            payload = result.result if isinstance(result.result, dict) else {}
            if result.successful() and payload.get("success", True):
                report.status = ProblemDuplicateReport.SUCCESS
                report.candidate_count = int(
                    payload.get("count", report.candidate_count)
                )
                report.error = ""
            else:
                report.status = ProblemDuplicateReport.FAILED
                report.error = str(result.result)
            report.finished_at = timezone.now()
            report.save(
                update_fields=["status", "candidate_count", "error", "finished_at"]
            )
            return report

    if timezone.now() - report.started_at > timedelta(
        seconds=DUPLICATE_REPORT_REFRESH_TIMEOUT_SECONDS
    ):
        report.status = ProblemDuplicateReport.FAILED
        report.error = "Refresh task timed out or was not found in Celery."
        report.finished_at = timezone.now()
        report.save(update_fields=["status", "error", "finished_at"])
    return report


def get_duplicate_problem_merge_history(limit=50):
    return ProblemDuplicateReviewHistory.objects.select_related("actor").order_by(
        "-created_at"
    )[:limit]


def get_pending_duplicate_problem_merges():
    sync_pending_duplicate_problem_merges()
    return ProblemDuplicateMergeHistory.objects.select_related(
        "source_problem",
        "target_problem",
        "merged_by",
    ).filter(
        status__in=[
            ProblemDuplicateMergeHistory.PENDING,
            ProblemDuplicateMergeHistory.RUNNING,
        ]
    )


def get_done_duplicate_problem_merges(limit=50):
    sync_pending_duplicate_problem_merges()
    return ProblemDuplicateMergeHistory.objects.select_related(
        "source_problem",
        "target_problem",
        "merged_by",
    ).filter(
        status__in=[
            ProblemDuplicateMergeHistory.SUCCESS,
            ProblemDuplicateMergeHistory.FAILED,
        ]
    )[
        :limit
    ]


def record_duplicate_review_action(
    action,
    source_code,
    target_code,
    actor=None,
    source_id=None,
    target_id=None,
    details=None,
):
    return ProblemDuplicateReviewHistory.objects.create(
        action=action,
        source_code=source_code,
        target_code=target_code,
        source_problem_id_snapshot=source_id,
        target_problem_id_snapshot=target_id,
        actor=actor,
        details=details or {},
    )


def sync_pending_duplicate_problem_merges():
    for merge in ProblemDuplicateMergeHistory.objects.filter(
        status__in=[
            ProblemDuplicateMergeHistory.PENDING,
            ProblemDuplicateMergeHistory.RUNNING,
        ]
    ):
        _sync_pending_duplicate_problem_merge(merge)


def _sync_pending_duplicate_problem_merge(merge):
    if merge.task_id:
        try:
            result = AsyncResult(merge.task_id)
            ready = result.ready()
        except Exception:
            ready = False
        if ready:
            payload = result.result if isinstance(result.result, dict) else {}
            if result.successful() and payload.get("success", True):
                merge.status = ProblemDuplicateMergeHistory.SUCCESS
                merge.counts = payload.get("counts", merge.counts)
                merge.conflicts = payload.get("conflicts", merge.conflicts)
                merge.error = ""
                merge.merged_at = timezone.now()
                update_fields = [
                    "status",
                    "counts",
                    "conflicts",
                    "error",
                    "merged_at",
                ]
                if not merge.started_at:
                    merge.started_at = merge.requested_at
                    update_fields.append("started_at")
                merge.save(update_fields=update_fields)
                record_duplicate_review_action(
                    ProblemDuplicateReviewHistory.MERGED,
                    merge.source_code,
                    merge.target_code,
                    actor=merge.merged_by,
                    source_id=merge.source_problem_id_snapshot,
                    target_id=merge.target_problem_id_snapshot,
                    details={"counts": merge.counts, "conflicts": merge.conflicts},
                )
            else:
                _mark_duplicate_problem_merge_failed(
                    merge, _format_celery_task_error(result.result)
                )
            return merge

    if (
        merge.merged_at
        and merge.status != ProblemDuplicateMergeHistory.FAILED
        and not Problem.objects.filter(id=merge.source_problem_id_snapshot).exists()
    ):
        merge.status = ProblemDuplicateMergeHistory.SUCCESS
        merge.error = ""
        merge.save(update_fields=["status", "error"])
        record_duplicate_review_action(
            ProblemDuplicateReviewHistory.MERGED,
            merge.source_code,
            merge.target_code,
            actor=merge.merged_by,
            source_id=merge.source_problem_id_snapshot,
            target_id=merge.target_problem_id_snapshot,
            details={"counts": merge.counts, "conflicts": merge.conflicts},
        )
        return merge

    if timezone.now() - merge.requested_at > timedelta(
        seconds=DUPLICATE_MERGE_TIMEOUT_SECONDS
    ):
        _mark_duplicate_problem_merge_failed(
            merge,
            "Merge task timed out or was not found in Celery.",
        )
    return merge


def _mark_duplicate_problem_merge_failed(merge, error):
    merge.status = ProblemDuplicateMergeHistory.FAILED
    merge.error = error
    merge.merged_at = timezone.now()
    merge.save(update_fields=["status", "error", "merged_at"])
    record_duplicate_review_action(
        ProblemDuplicateReviewHistory.MERGE_FAILED,
        merge.source_code,
        merge.target_code,
        actor=merge.merged_by,
        source_id=merge.source_problem_id_snapshot,
        target_id=merge.target_problem_id_snapshot,
        details={"error": error, "task_id": merge.task_id},
    )
    return merge


def _format_celery_task_error(error):
    if not error:
        return "Celery task failed."
    error_type = error.__class__.__name__
    error_message = str(error)
    if error_message:
        return f"Celery task failed: {error_type}: {error_message}"
    return f"Celery task failed: {error_type}"


def record_duplicate_problem_merge(report, user=None, username=None):
    source = report.get("source", {})
    target = report.get("target", {})
    source_id = source.get("id")
    target_id = target.get("id")
    return ProblemDuplicateMergeHistory.objects.create(
        source_problem_id=_existing_problem_id(source_id),
        target_problem_id=_existing_problem_id(target_id),
        source_problem_id_snapshot=source_id,
        target_problem_id_snapshot=target_id,
        source_code=source.get("code", ""),
        target_code=target.get("code", ""),
        source_name=source.get("name", ""),
        target_name=target.get("name", ""),
        merged_by=user,
        counts=report.get("counts", {}),
        conflicts=report.get("conflicts", {}),
        status=ProblemDuplicateMergeHistory.SUCCESS,
        merged_at=timezone.now(),
    )


def create_pending_duplicate_problem_merge(source, target, user=None, task_id=""):
    sync_pending_duplicate_problem_merges()
    existing = ProblemDuplicateMergeHistory.objects.filter(
        source_code=source.code,
        target_code=target.code,
        status__in=[
            ProblemDuplicateMergeHistory.PENDING,
            ProblemDuplicateMergeHistory.RUNNING,
        ],
    ).first()
    if existing:
        raise DuplicateProblemMergePending("duplicate problem merge is pending")

    dry_run = None
    try:
        from judge.utils.problem_merge import ProblemMerge

        dry_run = ProblemMerge(source.code, target.code).run()
    except Exception:
        dry_run = {}
    merge = ProblemDuplicateMergeHistory.objects.create(
        source_problem_id=source.id,
        target_problem_id=target.id,
        source_problem_id_snapshot=source.id,
        target_problem_id_snapshot=target.id,
        source_code=source.code,
        target_code=target.code,
        source_name=source.name,
        target_name=target.name,
        merged_by=user,
        counts=dry_run.get("counts", {}),
        conflicts=dry_run.get("conflicts", {}),
        status=ProblemDuplicateMergeHistory.PENDING,
        task_id=task_id,
    )
    record_duplicate_review_action(
        ProblemDuplicateReviewHistory.MERGE_QUEUED,
        source.code,
        target.code,
        actor=user,
        source_id=source.id,
        target_id=target.id,
    )
    return merge


def run_pending_duplicate_problem_merge(merge_id):
    from judge.utils.problem_merge import ProblemMerge

    merge = ProblemDuplicateMergeHistory.objects.get(id=merge_id)
    merge.status = ProblemDuplicateMergeHistory.RUNNING
    merge.started_at = timezone.now()
    merge.save(update_fields=["status", "started_at"])
    record_duplicate_review_action(
        ProblemDuplicateReviewHistory.MERGE_RUNNING,
        merge.source_code,
        merge.target_code,
        actor=merge.merged_by,
        source_id=merge.source_problem_id_snapshot,
        target_id=merge.target_problem_id_snapshot,
        details={"task_id": merge.task_id},
    )
    try:
        report = ProblemMerge(merge.source_code, merge.target_code, apply=True).run()
    except Exception as exc:
        merge.status = ProblemDuplicateMergeHistory.FAILED
        merge.error = str(exc)
        merge.merged_at = timezone.now()
        merge.save(update_fields=["status", "error", "merged_at"])
        record_duplicate_review_action(
            ProblemDuplicateReviewHistory.MERGE_FAILED,
            merge.source_code,
            merge.target_code,
            actor=merge.merged_by,
            source_id=merge.source_problem_id_snapshot,
            target_id=merge.target_problem_id_snapshot,
            details={"error": str(exc), "task_id": merge.task_id},
        )
        raise

    merge.status = ProblemDuplicateMergeHistory.SUCCESS
    merge.counts = report.get("counts", {})
    merge.conflicts = report.get("conflicts", {})
    merge.error = ""
    merge.merged_at = timezone.now()
    merge.save(update_fields=["status", "counts", "conflicts", "error", "merged_at"])
    record_duplicate_review_action(
        ProblemDuplicateReviewHistory.MERGED,
        merge.source_code,
        merge.target_code,
        actor=merge.merged_by,
        source_id=merge.source_problem_id_snapshot,
        target_id=merge.target_problem_id_snapshot,
        details={"counts": merge.counts, "conflicts": merge.conflicts},
    )
    return report


def _existing_problem_id(problem_id):
    if problem_id and Problem.objects.filter(id=problem_id).exists():
        return problem_id
    return None


def update_duplicate_problem_report_cache_after_merge(source_id, target_id):
    deleted, _ = ProblemDuplicateCandidate.objects.filter(
        source_problem_id_snapshot=source_id
    ).delete()
    deleted_target, _ = ProblemDuplicateCandidate.objects.filter(
        target_problem_id_snapshot=source_id
    ).delete()
    return deleted + deleted_target


def invalidate_duplicate_problem_report_cache():
    ProblemDuplicateCandidate.objects.all().delete()
    ProblemDuplicateReport.objects.filter(status=ProblemDuplicateReport.SUCCESS).update(
        candidate_count=0
    )


def store_duplicate_problem_report_candidates(report, candidates):
    with transaction.atomic():
        old_false_positive_keys = set(
            report.candidates.filter(
                status=ProblemDuplicateCandidate.FALSE_POSITIVE
            ).values_list("source_code", "target_code")
        )
        report.candidates.all().delete()
        candidate_rows = [
            candidate_to_model(
                report,
                candidate,
                is_false_positive=(
                    candidate["source"]["code"],
                    candidate["target"]["code"],
                )
                in old_false_positive_keys,
            )
            for candidate in candidates
        ]
        ProblemDuplicateCandidate.objects.bulk_create(candidate_rows)
        report.candidate_count = sum(
            1
            for candidate in candidate_rows
            if candidate.status == ProblemDuplicateCandidate.OPEN
        )
        report.save(update_fields=["candidate_count"])


def mark_duplicate_candidate_false_positive(source_code, target_code, user=None):
    updated = ProblemDuplicateCandidate.objects.filter(
        source_code=source_code,
        target_code=target_code,
        status=ProblemDuplicateCandidate.OPEN,
    ).update(
        status=ProblemDuplicateCandidate.FALSE_POSITIVE,
        reviewed_by=user,
        reviewed_at=timezone.now(),
    )
    if updated:
        record_duplicate_review_action(
            ProblemDuplicateReviewHistory.MARKED_NOT_DUPLICATE,
            source_code,
            target_code,
            actor=user,
            details={"updated_candidates": updated},
        )
    return updated


def candidate_to_model(report, candidate, is_false_positive=False):
    source = candidate["source"]
    target = candidate["target"]
    return ProblemDuplicateCandidate(
        report=report,
        source_problem_id=source["id"],
        target_problem_id=target["id"],
        source_problem_id_snapshot=source["id"],
        target_problem_id_snapshot=target["id"],
        source_code=source["code"],
        target_code=target["code"],
        source_name=source["name"],
        target_name=target["name"],
        source_submission_count=source["submission_count"],
        target_submission_count=target["submission_count"],
        score=candidate["score"],
        status=(
            ProblemDuplicateCandidate.FALSE_POSITIVE
            if is_false_positive
            else ProblemDuplicateCandidate.OPEN
        ),
    )


def format_candidate(candidate):
    return {
        "score": candidate.score,
        "source": {
            "id": candidate.source_problem_id_snapshot,
            "code": candidate.source_code,
            "name": candidate.source_name,
            "url": _problem_url(candidate.source_problem, candidate.source_code),
            "submission_count": candidate.source_submission_count,
        },
        "target": {
            "id": candidate.target_problem_id_snapshot,
            "code": candidate.target_code,
            "name": candidate.target_name,
            "url": _problem_url(candidate.target_problem, candidate.target_code),
            "submission_count": candidate.target_submission_count,
        },
    }


def format_report_state(report):
    status_map = {
        ProblemDuplicateReport.PENDING: "PENDING",
        ProblemDuplicateReport.SUCCESS: "SUCCESS",
        ProblemDuplicateReport.FAILED: "FAILED",
    }
    return {
        "status": status_map.get(report.status, "IDLE"),
        "task_id": report.task_id,
        "options": {
            "min_score": report.min_score,
            "limit": report.limit,
            "neighbors": report.neighbors,
        },
        "started_at": report.started_at.isoformat() if report.started_at else None,
        "finished_at": report.finished_at.isoformat() if report.finished_at else None,
        "count": report.candidate_count,
        "error": report.error,
    }


def _problem_url(problem, code):
    if problem is not None:
        return problem.get_absolute_url()
    return f"/problem/{code}"


def _generate_duplicate_problem_candidates(options):
    problem_ids = list(
        Problem.objects.filter(is_public=True, is_organization_private=False)
        .filter(id__in=_indexed_problem_ids())
        .order_by("id")
        .values_list("id", flat=True)
    )

    pair_scores = {}
    for problem_id in problem_ids:
        embedding = _stored_problem_embedding(problem_id)
        if embedding is None:
            continue
        for other_id, score in _semantic_vector_search(
            embedding, options.neighbors, exclude_id=problem_id
        ):
            if score < options.min_score:
                continue
            left_id, right_id = sorted((problem_id, other_id))
            current = pair_scores.get((left_id, right_id))
            if current is None or score > current:
                pair_scores[(left_id, right_id)] = score

    ranked_pairs = sorted(pair_scores.items(), key=lambda item: item[1], reverse=True)[
        : options.limit
    ]
    return _format_duplicate_candidates(ranked_pairs)


def _indexed_problem_ids():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT problem_id
            FROM {SEMANTIC_TABLE}
            WHERE model = %s AND dims = %s
            """,
            [get_semantic_model(), get_semantic_dims()],
        )
        return [int(row[0]) for row in cursor.fetchall()]


def _format_duplicate_candidates(ranked_pairs):
    problem_ids = [problem_id for pair, score in ranked_pairs for problem_id in pair]
    problems = {
        problem.id: problem for problem in Problem.get_cached_instances(*problem_ids)
    }
    submission_counts = _submission_counts(problem_ids)

    candidates = []
    for (left_id, right_id), score in ranked_pairs:
        left = problems.get(left_id)
        right = problems.get(right_id)
        if left is None or right is None:
            continue
        # right (larger id) = source (merged away); left (smaller id) = target (kept).
        # Merge enforces source_id > target_id, so this assignment is intentional.
        source = right
        target = left
        candidates.append(
            {
                "score": score,
                "source": _format_problem(source, submission_counts),
                "target": _format_problem(target, submission_counts),
            }
        )
    return candidates


def _submission_counts(problem_ids):
    rows = (
        Submission.objects.filter(problem_id__in=problem_ids)
        .values("problem_id")
        .annotate(count=Count("id"))
    )
    return {row["problem_id"]: row["count"] for row in rows}


def _format_problem(problem, submission_counts):
    return {
        "id": problem.id,
        "code": problem.code,
        "name": problem.name,
        "url": problem.get_absolute_url(),
        "submission_count": submission_counts.get(problem.id, 0),
    }
