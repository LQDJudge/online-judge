"""duplicate_detection: semantic similarity vs existing public problems."""

import logging

from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

from judge.ml.semantic_search import (
    SemanticSearchUnavailable,
    similar_problems,
)
from judge.models.problem_review import ProblemReviewCheckResult
from judge.review.base import ProblemReviewCheck, CheckResultData

logger = logging.getLogger(__name__)


class DuplicateDetectionCheck(ProblemReviewCheck):
    id = "duplicate_detection"
    display_name = gettext_lazy("Duplicates")

    def run(self, problem, run):
        if not getattr(settings, "USE_ML", False):
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_(
                    "USE_ML is disabled — semantic duplicate detection unavailable."
                ),
            )

        threshold = getattr(settings, "AUTO_REVIEW_DUPLICATE_THRESHOLD", 0.93)
        try:
            matches = similar_problems(problem, limit=5)
        except SemanticSearchUnavailable as exc:
            return CheckResultData(
                status=ProblemReviewCheckResult.SKIPPED,
                reason=_("Semantic search unavailable: %(error)s") % {"error": exc},
            )
        except Exception as exc:
            logger.exception("duplicate_detection: similar_problems failed")
            return CheckResultData(
                status=ProblemReviewCheckResult.ERROR,
                reason=_("Semantic search error: %(error)s") % {"error": exc},
            )

        hot = [
            m
            for m in matches
            if m.get("score", 0.0) >= threshold and m.get("code") != problem.code
        ]
        if hot:
            return CheckResultData(
                status=ProblemReviewCheckResult.FAIL,
                reason=_(
                    "Found %(count)d similar public problem(s) above threshold %(threshold).2f."
                )
                % {
                    "count": len(hot),
                    "threshold": threshold,
                },
                details={"matches": hot, "threshold": threshold},
            )
        return CheckResultData(
            status=ProblemReviewCheckResult.SUCCESS,
            reason=_("No similar public problem above threshold %(threshold).2f.")
            % {"threshold": threshold},
            details={"matches": [], "threshold": threshold},
        )
