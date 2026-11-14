"""Review content identity and execution evidence."""

import os
from datetime import datetime

from judge.auto_review.context_utils import (
    _normalized_text,
    canonical_json,
    digest_json,
)


def _review_solutions(snapshot, include_evidence=False):
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


def problem_review_content(snapshot):
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
            key: _review_artifact_identity(value)
            for key, value in snapshot.get("artifacts", {}).items()
            if not key.startswith("aux:")
        },
        "solutions": _review_solutions(snapshot),
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
            _review_artifact_identity(row.get("artifact"))
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


def _review_artifact_identity(artifact):
    if not artifact:
        return None
    return {
        "revision": artifact.get("revision", artifact.get("sha256")),
        "size": artifact.get("size"),
        # Extension can select a runtime/parser even when bytes are unchanged.
        "suffix": os.path.splitext(artifact.get("filename", ""))[1].lower(),
    }


def reviewed_content_hash(run):
    """The identity accepted at admission; never reconstructed from current files."""
    return run.input_hash


def contest_review_content(snapshot):
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
        "children": canonical_child_content(snapshot.get("children", [])),
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


def problem_review_input_hash(snapshot):
    return digest_json(
        {
            "content_hash": digest_json(problem_review_content(snapshot)),
            "evidence": review_execution_evidence(snapshot),
        }
    )


def review_execution_evidence(snapshot):
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
                "artifact": _review_artifact_identity(row.get("artifact")),
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


def canonical_child_content(children):
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
