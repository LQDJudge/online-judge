import logging

from django.conf import settings

from celery import shared_task

from judge.ml.problem_duplicates import (
    DuplicateProblemReportOptions,
    get_or_compute_duplicate_problem_candidates,
    mark_duplicate_problem_report_refresh_finished,
    run_pending_duplicate_problem_merge,
)
from judge.ml.semantic_search import index_problem_embedding, prune_problem_embedding
from judge.models import Problem

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def index_problem_semantic_embedding(self, problem_id, force=False):
    if not getattr(settings, "USE_ML", False):
        return {"indexed": False, "skipped": True, "reason": "USE_ML is disabled"}

    try:
        problem = Problem.objects.get(id=problem_id)
    except Problem.DoesNotExist:
        prune_problem_embedding(problem_id)
        return {"indexed": False, "pruned": True, "reason": "problem does not exist"}

    try:
        return index_problem_embedding(problem, force=force)
    except Exception as exc:
        logger.error(
            "Failed to index semantic embedding for problem %s: %s",
            problem_id,
            exc,
            exc_info=True,
        )
        return {"indexed": False, "error": str(exc)}


@shared_task(bind=True)
def refresh_duplicate_problem_report(self, report_id):
    if not getattr(settings, "USE_ML", False):
        result = {"success": False, "error": "USE_ML is disabled"}
        mark_duplicate_problem_report_refresh_finished("FAILED", report_id, **result)
        return result

    from judge.models import ProblemDuplicateReport

    report = ProblemDuplicateReport.objects.get(id=report_id)
    options = DuplicateProblemReportOptions(
        min_score=report.min_score,
        limit=report.limit,
        neighbors=report.neighbors,
    )
    try:
        candidates = get_or_compute_duplicate_problem_candidates(
            force=True,
            options=options,
            report=report,
        )
    except Exception as exc:
        logger.error(
            "Failed to refresh duplicate problem report: %s", exc, exc_info=True
        )
        result = {"success": False, "error": str(exc)}
        mark_duplicate_problem_report_refresh_finished("FAILED", report_id, **result)
        raise

    result = {"success": True, "count": len(candidates), "options": options.as_dict()}
    mark_duplicate_problem_report_refresh_finished("SUCCESS", report_id, **result)
    return result


@shared_task(bind=True)
def merge_duplicate_problem(self, merge_id):
    return run_pending_duplicate_problem_merge(merge_id)
