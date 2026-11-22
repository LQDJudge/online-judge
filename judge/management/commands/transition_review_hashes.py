"""One-time transition of provably current review identities.

Run with web and workers stopped. The default is a read-only preview.
"""

import hashlib
import json
import os
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judge.auto_review.contest_context import build_contest_context
from judge.auto_review.execution_freshness import solution_execution_hash
from judge.auto_review.problem_context import MAX_PROFILE_CONFIG_BYTES, _capture_problem
from judge.auto_review.profile_config import parse_profile_config
from judge.auto_review.signature import relevant_signature_config
from judge.models.problem_data import problem_data_storage
from judge.auto_review.context_utils import (
    _normalized_text,
    canonical_json,
    digest_json,
)
from judge.models import (
    ContestPublicRequest,
    ContestReviewRun,
    Problem,
    ProblemData,
    ProblemTestCase,
    ProblemReviewRun,
    ProblemSolutionCode,
    PublicRequest,
    Submission,
    SubmissionSource,
)

# Frozen previous calculations, used only by this one-time data transition.


def _old_review_solutions(snapshot, include_evidence=False):
    fields = ("source", "language", "expected_result")
    if include_evidence:
        fields += ("evidence",)
    return sorted(
        [
            {key: code.get(key) for key in fields}
            for code in snapshot.get("solutions", [])
        ],
        key=canonical_json,
    )


def _old_problem_review_content(snapshot):
    """Technical review identity, independent of display and admin metadata."""
    config = snapshot.get("config", {})
    translations = [
        {
            "language": row.get("language"),
            "description": _normalized_text(row.get("description")),
        }
        for row in snapshot.get("translations", [])
    ]
    editorial = snapshot.get("editorial") or {}
    content = {
        "statement": _normalized_text(snapshot.get("statement")),
        "config": {
            key: config.get(key)
            for key in (
                "time_limit",
                "memory_limit",
                "points",
                "partial",
                "short_circuit",
            )
        },
        "file_revisions": snapshot.get("file_revisions", {}),
        "data_config": snapshot.get("data_config", {}),
        # List order and batch markers are meaningful; database row IDs are not.
        "tests": [
            {
                key: value
                for key, value in row.items()
                if key not in ("id", "dataset_id")
            }
            for row in snapshot.get("tests", [])
        ],
        "artifacts": {
            key: _old_review_artifact_identity(value)
            for key, value in snapshot.get("artifacts", {}).items()
            if not key.startswith("aux:")
        },
        "solutions": _old_review_solutions(snapshot),
        "editorial": _normalized_text(editorial.get("content")),
        "language_limits": sorted(
            [
                {
                    key: value
                    for key, value in row.items()
                    if key not in ("id", "problem_id")
                }
                for row in snapshot.get("language_limits", [])
            ],
            key=canonical_json,
        ),
        "allowed_languages": sorted(
            snapshot.get("roles", {}).get("allowed_languages", [])
        ),
        "language_templates": sorted(
            [
                {key: row.get(key) for key in ("language", "source")}
                for row in snapshot.get("language_templates", [])
            ],
            key=canonical_json,
        ),
        "translations": sorted(translations, key=canonical_json),
    }
    if snapshot.get("data_config", {}).get("use_ioi_signature"):
        if snapshot.get("signature_init_identity"):
            content["artifacts"]["init_yml"] = snapshot["signature_init_identity"]
        content["signature_interfaces"] = sorted(
            [
                entry
                for entry in snapshot.get("signature_graders", [])
                if entry.get("active")
            ],
            key=canonical_json,
        )
    if "output_examples" in snapshot:
        examples = [
            _old_review_artifact_identity(row.get("artifact"))
            for row in snapshot["output_examples"]
        ]
        content["output_examples"] = [
            value
            for _, value in sorted(
                {canonical_json(value): value for value in examples}.items()
            )
        ]
    if "dataset_descriptions" in snapshot:
        content["dataset_descriptions"] = snapshot["dataset_descriptions"]
    return content


def _old_review_artifact_identity(artifact):
    if not artifact:
        return None
    return {
        "revision": artifact.get("revision", artifact.get("sha256")),
        "size": artifact.get("size"),
        # Extension can select a runtime/parser even when bytes are unchanged.
        "suffix": os.path.splitext(artifact.get("filename", ""))[1].lower(),
    }


def _old_contest_review_content(snapshot):
    """Technical contest identity, excluding team and exposure metadata.

    Absolute scheduling is operational. Workload and timing rules depend on the
    duration, participant time limit and freeze interval, not the calendar date.
    Invalid/unparseable timing retains its original values rather than matching
    a previously valid contest. Item list order matters; row IDs/order labels do
    not.
    """
    config = snapshot.get("config", {})
    try:
        duration = {
            # The contest editor schedules to whole seconds. Ignore accidental
            # microseconds from programmatic creation/form round trips.
            "duration_seconds": round(
                (
                    datetime.fromisoformat(config["end_time"])
                    - datetime.fromisoformat(config["start_time"])
                ).total_seconds()
            )
        }
    except (KeyError, TypeError, ValueError):
        duration = {key: config.get(key) for key in ("start_time", "end_time")}
    quiz_content = snapshot.get("quiz_content", {})
    return {
        "config": {
            "description": _normalized_text(config.get("description")),
            **duration,
            **{
                key: config.get(key)
                for key in (
                    "time_limit",
                    "freeze_after",
                    "format_name",
                    "format_config",
                    "points_precision",
                    "run_pretests_only",
                    "scoreboard_visibility",
                    "public_scoreboard",
                    "use_clarifications",
                    "hide_problem_tags",
                    "problem_label_script",
                    "rate_limit",
                )
            },
        },
        "items": [
            {
                key: item.get(key)
                for key in (
                    "problem_id",
                    "quiz_id",
                    "points",
                    "initial_ac_score",
                    "partial",
                    "is_pretested",
                    "show_testcases",
                    "max_submissions",
                    "hidden_subtasks",
                    "is_result_hidden",
                )
            }
            for item in snapshot.get("items", [])
        ],
        "children": _old_canonical_child_content(snapshot.get("children", [])),
        # Unsupported quizzes remain manual, but substantive edits must still
        # invalidate the contest candidate. Do not bind their display/admin data.
        "quiz_content": {
            "quizzes": sorted(
                [
                    {
                        **{
                            key: quiz.get(key)
                            for key in (
                                "id",
                                "time_limit",
                                "shuffle_questions",
                                "is_shown_correctness",
                                "is_shown_answer",
                            )
                        },
                        "description": _normalized_text(quiz.get("description")),
                    }
                    for quiz in quiz_content.get("quizzes", [])
                ],
                key=canonical_json,
            ),
            "assignments": [
                {key: row.get(key) for key in ("quiz_id", "question_id", "points")}
                for row in quiz_content.get("assignments", [])
            ],
            "questions": sorted(
                [
                    {
                        key: question.get(key)
                        for key in (
                            "id",
                            "question_type",
                            "content",
                            "choices",
                            "correct_answers",
                            "explanation",
                            "shuffle_choices",
                            "grading_strategy",
                            "multiple_true_false_score_table",
                        )
                    }
                    for question in quiz_content.get("questions", [])
                ],
                key=canonical_json,
            ),
        },
    }


def _old_problem_review_input_hash(snapshot):
    return digest_json(
        {
            "content_hash": digest_json(_old_problem_review_content(snapshot)),
            "evidence": _old_review_execution_evidence(snapshot),
        }
    )


def _old_review_execution_evidence(snapshot):
    """Meaningful completed outcomes, not record IDs, timestamps or runtime noise.

    Policy and provider changes are maintainer-managed, never automatic
    invalidation.
    """

    def outcome(evidence, validation=False):
        if not evidence or evidence.get("status") not in ("D", "CE"):
            return None
        if not validation and evidence.get("result") in ("IE", "AB"):
            return None
        keys = (
            ("passed", "total_cases", "failed_count")
            if validation
            else ("result", "points", "case_points", "case_total", "is_pretested")
        )
        return {
            **{key: evidence.get(key) for key in keys},
            "cases": sorted(
                [
                    {
                        key: row.get(key)
                        for key in ("case", "batch", "status", "points", "total")
                    }
                    for row in evidence.get("cases", [])
                ],
                key=canonical_json,
            ),
        }

    result = {
        "solutions": sorted(
            [
                {
                    "source": code.get("source"),
                    "language": code.get("language"),
                    "expected_result": code.get("expected_result"),
                    "outcome": outcome(code.get("evidence")),
                }
                for code in snapshot.get("solutions", [])
            ],
            key=lambda row: canonical_json(
                {key: row[key] for key in ("source", "language", "expected_result")}
            ),
        ),
        "validation": outcome(snapshot.get("validation"), validation=True),
    }
    if "output_examples" in snapshot:
        observations = [
            {
                "artifact": _old_review_artifact_identity(row.get("artifact")),
                "outcome": outcome(row.get("evidence")),
            }
            for row in snapshot["output_examples"]
        ]
        result["output_examples"] = [
            value
            for _, value in sorted(
                {canonical_json(value): value for value in observations}.items()
            )
        ]
    return result


def _old_canonical_child_content(children):
    """Review attachment order/repeated items cannot change content identity."""
    identities = {
        (child.get("problem_id"), child.get("content_hash")) for child in children
    }
    return [
        {"problem_id": identity, "content_hash": content_hash}
        for identity, content_hash in sorted(
            identities, key=lambda item: (item[0] or 0, item[1] or "")
        )
    ]


def _old_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _old_test_configuration(problem, *, validation=False):
    # One package unit, including generator arguments, scoring and test selection.
    fields = (
        ("order", "type", "input_file", "generator_args")
        if validation
        else [
            field.attname
            for field in ProblemTestCase._meta.concrete_fields
            if field.name not in {"id", "dataset"}
        ]
    )
    return list(
        ProblemTestCase.objects.filter(dataset_id=problem.pk)
        .order_by("order", "pk")
        .values(*fields)
    )


def _old_data(problem):
    return ProblemData.objects.filter(problem_id=problem.pk).values().first() or {}


def _old_solution_package_identity(
    problem, *, data=None, tests=None, language_limits=None, signature_graders=None
):
    data = _old_data(problem) if data is None else data
    excluded = {
        "id",
        "problem_id",
        "feedback",
        "validator_revision",
        "testcase_validator",
        "testcase_validator_language",
    }
    return {
        "data": {key: value for key, value in data.items() if key not in excluded},
        "tests": _old_test_configuration(problem) if tests is None else tests,
        "limits": {
            name: getattr(problem, name)
            for name in (
                "time_limit",
                "memory_limit",
                "points",
                "partial",
                "short_circuit",
            )
        },
        "language_limits": (
            list(
                problem.language_limits.order_by("language_id").values(
                    "language_id", "time_limit", "memory_limit"
                )
            )
            if language_limits is None
            else language_limits
        ),
        "signature_graders": (
            list(
                problem.signature_graders.order_by("pk").values(
                    "language", "handler", "header"
                )
            )
            if signature_graders is None
            else signature_graders
        ),
    }


def _old_solution_execution_hash(problem, source, language_id, *, package=None):
    return _old_digest(
        {
            "package": (
                package
                if package is not None
                else _old_solution_package_identity(problem)
            ),
            "source": source,
            "language": language_id,
        }
    )


def _old_solution_result_current(problem, solution, submission, *, package=None):
    return bool(
        submission
        and submission.execution_hash
        and submission.execution_hash
        == _old_solution_execution_hash(
            problem, solution.source_code, solution.language_id, package=package
        )
    )


class Command(BaseCommand):
    help = "Transition provably current review hashes; stop web and workers first."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        if (
            ProblemReviewRun.objects.filter(status__in=("R", "B")).exists()
            or ContestReviewRun.objects.filter(status__in=("R", "B")).exists()
        ):
            raise CommandError(
                "Stop and resolve running or blocked reviews before transitioning."
            )
        counts = Counter()
        identities = {}

        def problem_identities(problem):
            if problem.pk not in identities:
                context = _capture_problem(
                    problem,
                    include_execution=True,
                    extract_pdf=False,
                    identity_only=True,
                )
                old_context = dict(context)
                if context.get("signature_init_identity"):
                    with problem_data_storage.open(
                        f"{problem.code}/init.yml", "rb"
                    ) as stream:
                        config = parse_profile_config(
                            stream.read(MAX_PROFILE_CONFIG_BYTES + 1)
                        )
                    old_context["signature_init_identity"] = {
                        "configuration_sha256": digest_json(
                            relevant_signature_config(
                                config,
                                context["allowed_language_keys"],
                                include_pretests=True,
                            )
                        )
                    }
                identities[problem.pk] = (
                    _old_problem_review_input_hash(old_context),
                    context["input_hash"],
                )
            return identities[problem.pk]

        problem_updates = []
        for run in (
            ProblemReviewRun.objects.exclude(input_hash="")
            .select_related("problem")
            .iterator()
        ):
            old, new = problem_identities(run.problem)
            if run.input_hash == old:
                problem_updates.append((run, new))
            else:
                counts["stale problem reviews left unchanged"] += 1

        contest_updates = []
        for run in (
            ContestReviewRun.objects.exclude(input_hash="")
            .select_related("contest")
            .iterator()
        ):
            ids = list(
                run.contest.contest_problems.exclude(problem_id=None)
                .order_by("order", "id")
                .values_list("problem_id", flat=True)
            )
            problems = Problem.objects.in_bulk(ids)
            pairs = [(pid, problem_identities(problems[pid])) for pid in ids]
            old_children = [
                {"problem_id": pid, "content_hash": pair[0]} for pid, pair in pairs
            ]
            new_children = [
                {"problem_id": pid, "content_hash": pair[1]} for pid, pair in pairs
            ]
            context = build_contest_context(
                run.contest, old_children, identity_only=True
            )
            old = digest_json(_old_contest_review_content(context))
            if run.input_hash == old:
                new = build_contest_context(
                    run.contest, new_children, identity_only=True
                )["input_hash"]
                contest_updates.append((run, new, dict(pairs)))
            else:
                counts["stale contest reviews left unchanged"] += 1

        execution_updates = []
        for code in (
            ProblemSolutionCode.objects.filter(last_submission__isnull=False)
            .exclude(last_submission__execution_hash="")
            .select_related("problem", "last_submission")
            .iterator()
        ):
            submission = code.last_submission
            if _old_solution_result_current(code.problem, code, submission):
                execution_updates.append(
                    (
                        submission,
                        solution_execution_hash(
                            code.problem, code.source_code, code.language_id
                        ),
                    )
                )
            else:
                counts["stale solution results left unchanged"] += 1

        output_updates = []
        for submission in (
            Submission.objects.filter(
                language__key="OUTPUT",
                review_evidence__output_portfolio=True,
                status="D",
            )
            .exclude(result__in=("IE", "AB"))
            .exclude(execution_hash="")
            .select_related("problem", "source")
            .iterator()
        ):
            try:
                source = submission.source.source
            except SubmissionSource.DoesNotExist:
                counts["output results with missing source left unchanged"] += 1
                continue
            old = _old_solution_execution_hash(
                submission.problem, source, submission.language_id
            )
            if submission.execution_hash == old:
                output_updates.append(
                    (
                        submission,
                        solution_execution_hash(
                            submission.problem, source, submission.language_id
                        ),
                    )
                )
            else:
                counts["stale output results left unchanged"] += 1

        counts["current problem reviews"] = len(problem_updates)
        counts["current contest reviews"] = len(contest_updates)
        counts["current solution results"] = len(execution_updates)
        counts["current output results"] = len(output_updates)
        if options["apply"]:
            for run, new in problem_updates:
                PublicRequest.objects.filter(
                    review_run=run,
                    candidate_hash=run.input_hash,
                    status=PublicRequest.PENDING,
                ).update(candidate_hash=new)
                run.input_hash = new
                run.save(update_fields=["input_hash"])
            for run, new, pairs in contest_updates:
                ContestPublicRequest.objects.filter(
                    review_run=run,
                    candidate_hash=run.input_hash,
                    status=ContestPublicRequest.PENDING,
                ).update(candidate_hash=new)
                for dependency in run.child_dependencies.all():
                    pair = pairs.get(dependency.problem_id)
                    if pair and dependency.content_hash == pair[0]:
                        dependency.content_hash = pair[1]
                        dependency.save(update_fields=["content_hash"])
                run.input_hash = new
                run.save(update_fields=["input_hash"])
            for submission, new in execution_updates + output_updates:
                submission.execution_hash = new
                submission.save(update_fields=["execution_hash"])
        for name, count in sorted(counts.items()):
            self.stdout.write(f"{name}: {count}")
        self.stdout.write(
            "Applied." if options["apply"] else "Preview only; no changes made."
        )
