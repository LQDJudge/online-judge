"""
Ordered list of checks executed by the runner.

Add new checks here in the desired execution order. Cheap checks first
(deterministic / no I/O), expensive checks last (LLM / judge submissions).
"""

from judge.review.checks.artifacts_present import ArtifactsPresentCheck
from judge.review.checks.checker_correctness import CheckerCorrectnessCheck
from judge.review.checks.duplicate_detection import DuplicateDetectionCheck
from judge.review.checks.solutions_rubric import SolutionsRubricCheck
from judge.review.checks.time_limit_headroom import TimeLimitHeadroomCheck
from judge.review.checks.validator_runs_clean import ValidatorRunsCleanCheck

CHECKS = [
    ArtifactsPresentCheck(),
    DuplicateDetectionCheck(),
    SolutionsRubricCheck(),
    CheckerCorrectnessCheck(),
    TimeLimitHeadroomCheck(),
    ValidatorRunsCleanCheck(),
]
