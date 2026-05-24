import logging

from celery import shared_task
from django.conf import settings

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
