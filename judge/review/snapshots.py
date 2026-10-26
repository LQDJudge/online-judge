"""Private, content-addressed package snapshots for the coordinator.

No snapshot points at an author's overwriteable upload. Artifact URLs deliberately
do not exist: callers must authorize the review before reading these bytes.
"""

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files import File
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models import Count, F, Max, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import (
    Problem,
    ProblemAttachment,
    Submission,
    SubmissionSource,
    SubmissionTestCase,
)
from judge.contest_format import formats
from judge.review.pdf_statement import extract_pdf_statement
from judge.review.profile_config import (
    MAX_PROFILE_CONFIG_BYTES,
    ProfileConfigError,
    parse_profile_config,
    mismatched_judging_components,
)
from judge.review.types import resolve_problem_profile, snapshot_problem_profile
from judge.review.signature import (
    enabled_signature_families,
    signature_family,
    relevant_signature_config,
)
from judge.review.artifact_readers import (
    MAX_OUTPUT_EXAMPLE_BYTES,
    SAFE_ZIP_COMPRESSION,
    UnsupportedArchiveCompression,
    open_output_source,
    summarize_data,
)
from judge.review.checker_options import checker_configurations
from judge.models.problem import (
    LanguageLimit,
    LanguageTemplate,
    ProblemTranslation,
    Solution,
)
from judge.models.problem_data import (
    ProblemData,
    ProblemSolutionCode,
    ProblemSignatureGrader,
    ProblemTestCase,
    ProblemValidation,
    problem_data_storage,
)
from judge.models.quiz import Quiz, QuizAttempt, QuizQuestion, QuizQuestionAssignment

POLICY_VERSION = 1
FILE_FIELDS = (
    "zipfile",
    "generator",
    "custom_checker",
    "custom_checker_cpp",
    "interactive_judge",
    "testcase_validator",
    "signature_handler",
    "signature_header",
    "communication_manager",
)
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024


class SnapshotError(Exception):
    """A snapshot cannot safely certify a coherent, readable package."""


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_json(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _normalized_text(value):
    # Preserve significant indentation and spaces; normalize only newline encoding.
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


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
    """Technical review identity, independent of display/admin/database metadata.

    This projection also works on historical snapshots without rewriting their
    frozen hashes, source, findings or execution attestations.
    """
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
        **{key: artifact.get(key) for key in ("sha256", "size")},
        # Extension can select a runtime/parser even when bytes are unchanged.
        "suffix": os.path.splitext(artifact.get("filename", ""))[1].lower(),
    }


def reviewed_content_hash(run):
    snapshot = run.snapshot
    # Minimal/legacy non-coordinator records have no reconstructable package.
    if (
        snapshot.get("kind") == "problem"
        and "statement" in snapshot
        and "config" in snapshot
    ):
        return digest_json(problem_review_content(snapshot))
    if snapshot.get("kind") == "contest" and all(
        key in snapshot for key in ("config", "items", "children")
    ):
        # Reproject exact captured child reports when the technical hash schema
        # changes. Never substitute a newer child or rewrite the saved binding.
        if snapshot.get("content_hash_version", 0) < 3 and hasattr(
            run, "child_dependencies"
        ):
            rows = getattr(run, "_review_dependency_rows", None)
            if rows is None:
                rows = list(
                    run.child_dependencies.values(
                        "problem_id",
                        "content_hash",
                        "problem_run__snapshot",
                        "problem_run__content_hash",
                    )
                )
            child_hashes = {}
            for row in rows:
                child_snapshot = row.get("problem_run__snapshot") or {}
                if (
                    child_snapshot.get("kind") == "problem"
                    and "statement" in child_snapshot
                    and "config" in child_snapshot
                ):
                    normalized = digest_json(problem_review_content(child_snapshot))
                    # Only upgrade identities known to name this exact child.
                    if row.get("content_hash") in (
                        row.get("problem_run__content_hash"),
                        normalized,
                    ):
                        child_hashes[(row["problem_id"], row["content_hash"])] = (
                            normalized
                        )
            snapshot = {
                **snapshot,
                "children": [
                    {
                        **child,
                        "content_hash": child_hashes.get(
                            (child.get("problem_id"), child.get("content_hash")),
                            child.get("content_hash"),
                        ),
                    }
                    for child in snapshot["children"]
                ],
            }
        return digest_json(contest_review_content(snapshot))
    return run.content_hash


def contest_review_content(snapshot):
    """Technical contest identity; full team/display/exposure evidence stays frozen.

    Absolute scheduling is operational. Workload and timing rules depend on the
    duration, participant time limit and freeze interval, not the calendar date.
    Invalid/unparseable timing retains its original values rather than matching
    a previously valid contest. Item list order matters; row IDs/order labels do
    not. This projection is also applied to historical coordinator snapshots.
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


def normalized_dependency_hash(content_hash, problem_run):
    if problem_run and content_hash == problem_run.content_hash:
        return reviewed_content_hash(problem_run)
    return content_hash


def problem_review_input_hash(snapshot):
    return digest_json(
        {
            "content_hash": digest_json(problem_review_content(snapshot)),
            "evidence": review_execution_evidence(snapshot),
        }
    )


def review_execution_evidence(snapshot):
    """Meaningful completed outcomes, not record IDs, timestamps or runtime noise.

    Full raw measurements/provenance remain frozen in the snapshot. Policy and
    provider changes are maintainer-managed, never automatic invalidation.
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


def execution_results_changed(before, after):
    """Pending/infra attempts do not erase the last reviewed completed result."""
    previous, current = review_execution_evidence(before), review_execution_evidence(
        after
    )
    if current["validation"] is None:
        current["validation"] = previous["validation"]
    for old, new in zip(previous["solutions"], current["solutions"]):
        if new["outcome"] is None:
            new["outcome"] = old["outcome"]
    return previous != current


def legacy_execution_package_identity(snapshot):
    """Only execution inputs, not statement or publication metadata."""
    config = dict(snapshot.get("config", {}))
    config.pop("date", None)
    return {
        "config": config,
        "data_config": snapshot.get("data_config", {}),
        "tests": snapshot.get("tests", []),
        "artifacts": {
            key: _artifact_identity(value)
            for key, value in snapshot.get("artifacts", {}).items()
        },
        "language_limits": snapshot.get("language_limits", []),
    }


def execution_package_identity(snapshot):
    """Judge inputs only; PDFs, templates and display metadata are not executed."""
    content = problem_review_content(snapshot)
    identity = {
        "config": content["config"],
        "data_config": content["data_config"],
        "tests": content["tests"],
        "artifacts": {
            key: value
            for key, value in content["artifacts"].items()
            if key != "pdf_description" and not key.startswith("dataset:")
        },
        "language_limits": content["language_limits"],
    }
    if "signature_interfaces" in content:
        identity["signature_interfaces"] = content["signature_interfaces"]
    return identity


def review_artifact_storage():
    root = getattr(
        settings,
        "REVIEW_ARTIFACT_ROOT",
        os.path.join(settings.BASE_DIR, "tmp", "review-artifacts"),
    )
    media = os.path.realpath(settings.MEDIA_ROOT)
    resolved = os.path.realpath(root)
    if media and os.path.commonpath([media, resolved]) == media:
        raise SnapshotError(
            _("Review artifact storage must be outside public media storage.")
        )
    return FileSystemStorage(
        location=resolved,
        base_url=None,
        file_permissions_mode=0o600,
        directory_permissions_mode=0o700,
    )


def freeze_file(field, persist=True, *, max_bytes=None):
    if not field or not field.name:
        return None
    maximum = int(getattr(settings, "REVIEW_MAX_ARTIFACT_BYTES", MAX_ARTIFACT_BYTES))
    if max_bytes is not None:
        maximum = min(maximum, max_bytes)
    storage = review_artifact_storage() if persist else None
    h = hashlib.sha256()
    size = 0
    with tempfile.TemporaryFile() as temporary:
        with field.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                size += len(chunk)
                if size > maximum:
                    raise SnapshotError(
                        _("An artifact exceeds the safe review snapshot size.")
                    )
                h.update(chunk)
                if persist:
                    temporary.write(chunk)
        digest = h.hexdigest()
        name = "sha256/%s" % digest
        if persist and not storage.exists(name):
            temporary.seek(0)
            # Concurrent identical writes may receive a suffix. The returned name
            # is immutable and verified on read, rather than assuming save's name.
            name = storage.save(name, File(temporary))
    return {
        "backend": "review-private",
        "storage_name": name,
        "sha256": digest,
        "size": size,
        "filename": os.path.basename(field.name),
    }


def read_artifact(artifact, max_bytes=None):
    name = artifact.get("storage_name", "")
    if (
        artifact.get("backend") != "review-private"
        or not name.startswith("sha256/")
        or "/" in name[7:]
        or ".." in name
    ):
        raise SnapshotError(_("Invalid review artifact reference."))
    limit = int(
        max_bytes
        if max_bytes is not None
        else getattr(settings, "REVIEW_MAX_ARTIFACT_BYTES", MAX_ARTIFACT_BYTES)
    )
    if artifact.get("size", limit + 1) > limit:
        raise SnapshotError(_("Review artifact exceeds this operation's input limit."))
    with review_artifact_storage().open(name, "rb") as source:
        data = source.read(limit + 1)
    if (
        len(data) > limit
        or len(data) != artifact["size"]
        or hashlib.sha256(data).hexdigest() != artifact["sha256"]
    ):
        raise SnapshotError(_("Review artifact identity verification failed."))
    return data


def _scalars(instance, excluded=()):
    return {
        field.attname: _json_value(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields
        if field.name not in excluded and not isinstance(field, models.FileField)
    }


def _artifact_identity(artifact):
    return (
        {key: artifact[key] for key in ("sha256", "size", "filename")}
        if artifact
        else None
    )


def _zip_inventory(field, tests):
    if not field:
        return {
            "kind": "generator",
            "configured_cases": sum(row["type"] == "C" for row in tests),
            "limitations": [
                "Generated official input bytes need immutable judge evidence; source alone is not observed coverage."
            ],
        }
    try:
        with field.open("rb") as stream, zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            members = {entry.filename: entry for entry in entries}
            errors = []
            if len(entries) != len(members):
                errors.append("Duplicate ZIP member names are ambiguous.")
            records = []
            for row in tests:
                if row["type"] != "C":
                    continue
                record = {"id": str(row["id"]), "ref": "test:%s" % row["id"]}
                for kind in ("input", "output"):
                    name = row.get(kind + "_file", "")
                    entry = members.get(name)
                    if not entry:
                        if not row.get("generator_args"):
                            errors.append(
                                "Missing %s ZIP member for test:%s." % (kind, row["id"])
                            )
                        continue
                    if entry.flag_bits & 1:
                        errors.append("Encrypted ZIP member for test:%s." % row["id"])
                        continue
                    record[kind + "_size"] = entry.file_size
                    if entry.compress_type not in SAFE_ZIP_COMPRESSION:
                        record[kind + "_preview_unavailable"] = (
                            "ZIP compression is not supported by the bounded review reader."
                        )
                        continue
                    with archive.open(entry) as case_stream:
                        preview = case_stream.read(4097)
                    if len(preview) <= 4096:
                        record[kind + "_full_small"] = preview.decode("utf8", "replace")
                    else:
                        record[kind + "_preview"] = preview[:512].decode(
                            "utf8", "replace"
                        )
                records.append(record)
            return {
                "kind": "zip",
                "configured_cases": len(records),
                "zip_members": len(entries),
                "records": records,
                "errors": errors,
                "limitations": [
                    "Large-file previews are not complete coverage statistics; dimensions require validated parsing."
                ],
            }
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SnapshotError(
            _("The testcase archive is unreadable or corrupt.")
        ) from exc


def _freeze_auxiliary_sources(data, code, persist, strict=True, extra_roots=()):
    if data is None:
        return {}, []
    artifacts = {}
    limitations = []
    pending = []
    keys = ["generator", "testcase_validator", "custom_checker_cpp"]
    if data.checker in ("interact", "interacttl"):
        keys.append("interactive_judge")
    if data.communication_manager and data.communication_num_processes:
        keys.append("communication_manager")
    roots = [getattr(data, key) for key in keys] + list(extra_roots)
    for field in roots:
        if field:
            try:
                with field.open("rb") as stream:
                    pending.append(stream.read(MAX_SOURCE_BYTES + 1))
            except OSError:
                if strict:
                    raise
                limitations.append("An auxiliary-source root is missing or unreadable.")
    checked = set()
    while pending:
        source = pending.pop()
        for raw_name in re.findall(
            rb'^\s*#\s*include\s*"([^"\r\n]+)"', source, re.MULTILINE
        ):
            name = raw_name.decode("utf8", "replace")
            if name in checked:
                continue
            checked.add(name)
            if (
                len(checked) > 16
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                or name.startswith(".")
            ):
                limitations.append(
                    "Quoted include could not be safely captured: " + name
                )
                continue
            path = "%s/%s" % (code, name)
            if not problem_data_storage.exists(path):
                limitations.append(
                    "Quoted include was not in the package; judge-installed dependency may be required: "
                    + name
                )
                continue
            try:
                with problem_data_storage.open(path, "rb") as stream:
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                    if len(raw) > MAX_SOURCE_BYTES:
                        limitations.append(
                            "Quoted include exceeds the review source limit: " + name
                        )
                        continue
                    stream.seek(0)
                    artifacts["aux:" + name] = freeze_file(stream, persist)
                    pending.append(raw)
            except OSError:
                if strict:
                    raise
                artifacts["aux:" + name] = {
                    "filename": name,
                    "sha256": None,
                    "size": None,
                    "unavailable": True,
                }
    return artifacts, limitations


def _manifest_artifact(field, persist, strict=True):
    try:
        return freeze_file(field, persist)
    except (OSError, SnapshotError):
        if strict:
            raise
        return {
            "filename": os.path.basename(field.name),
            "sha256": None,
            "size": None,
            "unavailable": True,
            "reason": "missing_or_unreadable",
        }


def _capture_problem(problem, persist, include_execution=True, extract_pdf=True):
    # This is an integrity boundary: read actual rows, not possibly stale cached
    # objects. Queries are batched, including the portfolio's per-case evidence.
    fresh = Problem.objects.get(pk=problem.pk)
    data = ProblemData.objects.filter(problem_id=fresh.pk).first()
    config = _scalars(
        fresh, ("id", "code", "description", "name", "pdf_description", "is_locked")
    )
    # Activity/popularity does not change a problem's mathematical package.
    for key in (
        "user_count",
        "ac_rate",
        "is_manually_managed",
        "created",
        "modified",
        "last_modified",
        "is_public",
        "is_organization_private",
    ):
        config.pop(key, None)
    artifacts = (
        {
            name: _manifest_artifact(getattr(data, name), persist, include_execution)
            for name in FILE_FIELDS
        }
        if data
        else {}
    )
    init_name = "%s/init.yml" % fresh.code
    effective_config = None
    if problem_data_storage.exists(init_name):
        with problem_data_storage.open(init_name, "rb") as init_file:
            raw_config = init_file.read(MAX_PROFILE_CONFIG_BYTES + 1)
            init_file.seek(0)
            artifacts["init_yml"] = freeze_file(init_file, persist)
        if (
            len(raw_config) <= MAX_PROFILE_CONFIG_BYTES
            and hashlib.sha256(raw_config).hexdigest()
            != artifacts["init_yml"]["sha256"]
        ):
            raise SnapshotError(
                _("The judge configuration changed during snapshot capture.")
            )
        try:
            effective_config = parse_profile_config(raw_config)
        except ProfileConfigError:
            # Preserve the artifact and diagnose it in preparation, never treat
            # an unreadable effective mode as ordinary batch.
            effective_config = []
    artifacts["pdf_description"] = _manifest_artifact(
        fresh.pdf_description, persist, include_execution
    )
    runtime_keys = list(
        fresh.allowed_languages.order_by("key").values_list("key", flat=True)
    )
    signature_init_identity = None
    if data and data.use_ioi_signature and isinstance(effective_config, dict):
        try:
            signature_init_identity = {
                "configuration_sha256": digest_json(
                    relevant_signature_config(effective_config, runtime_keys)
                ),
                "identity_version": 1,
            }
        except (TypeError, ValueError):
            raise SnapshotError(
                _("The signature judge configuration contains unsupported values.")
            )
    signature_graders = []
    signature_roots = []
    if data and data.use_ioi_signature:
        enabled_families = enabled_signature_families(runtime_keys)
        for grader in ProblemSignatureGrader.objects.filter(
            problem_id=fresh.pk
        ).order_by("language", "pk"):
            active = grader.language in enabled_families
            entry = {"family": grader.language, "active": active}
            if active:
                for component in ("handler", "header"):
                    field = getattr(grader, component)
                    key = "signature:%s:%s" % (grader.language, component)
                    artifacts[key] = _manifest_artifact(
                        field, persist, include_execution
                    )
                    entry[component] = key if artifacts[key] else None
                    if field:
                        signature_roots.append(field)
            signature_graders.append(entry)
    auxiliary, auxiliary_limits = _freeze_auxiliary_sources(
        data, fresh.code, persist, strict=include_execution, extra_roots=signature_roots
    )
    artifacts.update(auxiliary)
    tests = list(
        ProblemTestCase.objects.filter(dataset_id=fresh.pk)
        .order_by("order", "id")
        .values()
    )
    for row in tests:
        row.pop("dataset_id", None)
    codes = list(
        ProblemSolutionCode.objects.filter(problem_id=fresh.pk)
        .select_related("language", "last_submission")
        .order_by("order", "id")
    )
    submission_ids = (
        [code.last_submission_id for code in codes if code.last_submission_id]
        if include_execution
        else []
    )
    sources = dict(
        SubmissionSource.objects.filter(submission_id__in=submission_ids).values_list(
            "submission_id", "source"
        )
    )
    case_map = {sid: [] for sid in submission_ids}
    for result in (
        SubmissionTestCase.objects.filter(submission_id__in=submission_ids)
        .order_by("submission_id", "case")
        .values()
    ):
        case_map[result["submission_id"]].append(result)
    solutions = []
    for index, code in enumerate(codes):
        submission = code.last_submission if include_execution else None
        evidence = _scalars(submission) if submission else None
        if evidence:
            evidence.update(
                {
                    "source_hash": hashlib.sha256(
                        sources.get(submission.pk, "").encode()
                    ).hexdigest(),
                    "cases": case_map[submission.pk],
                }
            )
        solutions.append(
            {
                "id": code.pk,
                "ref": "solution:%s" % (index + 1),
                "name": code.name,
                "source": code.source_code,
                "language": code.language.key,
                "language_id": code.language_id,
                "expected_result": code.expected_result,
                "order": code.order,
                "evidence": evidence,
            }
        )
    validation = (
        (
            ProblemValidation.objects.filter(problem_id=fresh.pk)
            .prefetch_related("results")
            .order_by("-date", "-id")
            .first()
        )
        if include_execution
        else None
    )
    validation_evidence = _scalars(validation) if validation else None
    if validation:
        validation_evidence["cases"] = [
            _scalars(result) for result in validation.results.all()
        ]
    editorial = (
        Solution.objects.filter(problem_id=fresh.pk)
        .values("content", "is_public")
        .first()
    )
    limits = list(
        LanguageLimit.objects.filter(problem_id=fresh.pk)
        .order_by("language_id")
        .values()
    )
    templates = [
        {"language": row["language__key"], "source": row["source"]}
        for row in LanguageTemplate.objects.filter(problem_id=fresh.pk)
        .order_by("language__key")
        .values("language__key", "source")
    ]
    roles = {
        name: sorted(getattr(fresh, name).values_list("pk", flat=True))
        for name in (
            "authors",
            "curators",
            "testers",
            "allowed_languages",
            "organizations",
        )
    }
    data_config = _scalars(data, ("id", "problem", "feedback")) if data else {}
    snapshot = {
        "kind": "problem",
        "policy_version": POLICY_VERSION,
        "problem_id": fresh.pk,
        "code": fresh.code,
        "statement": fresh.description or "",
        "name": fresh.name,
        "config": config,
        "data_config": data_config,
        "judging_profile": resolve_problem_profile(
            data_config, effective_config=effective_config, artifacts=artifacts
        ).as_dict(),
        "signature_graders": signature_graders,
        "signature_init_identity": signature_init_identity,
        "effective_profile_config": (
            {
                "checker": effective_config.get("checker", "standard"),
                **{
                    key: effective_config[key]
                    for key in ("interactive", "communication", "signature_grader")
                    if key in effective_config
                },
            }
            if isinstance(effective_config, dict)
            else {}
        ),
        "artifacts": artifacts,
        "auxiliary_limits": auxiliary_limits,
        "tests": tests,
        "solutions": solutions,
        "validation": validation_evidence,
        "editorial": editorial,
        "language_limits": limits,
        "language_templates": templates,
        "allowed_language_keys": runtime_keys,
        "roles": roles,
        "visibility": {
            "is_public": fresh.is_public,
            "is_organization_private": fresh.is_organization_private,
        },
    }
    snapshot["inventory"] = (
        _zip_inventory(data.zipfile if data else None, tests)
        if include_execution
        else {
            "available": False,
            "limitations": ["Inventory parsing omitted in content-only manifest."],
        }
    )
    instruction_path = os.path.join(
        settings.BASE_DIR, "templates", "docs", "test-data-instructions.en.md"
    )
    with open(instruction_path, "r", encoding="utf-8") as instruction_file:
        instruction_text = instruction_file.read()
    snapshot["test_data_instructions"] = {
        "ref": "instructions:test-data",
        "language": "en",
        "text": instruction_text,
        "sha256": hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    }
    snapshot["translations"] = list(
        ProblemTranslation.objects.filter(problem_id=fresh.pk)
        .order_by("language", "id")
        .values("language", "name", "description")
    )
    snapshot["attachments"] = [
        {
            "id": attachment.pk,
            "filename": attachment.filename,
            "description": attachment.description,
            "order": attachment.order,
            # Attachment packages belong to later problem-type support. Keep
            # inventory metadata, but do not read/freeze unused package files.
            "artifact": None,
            "review_support": "deferred",
        }
        for attachment in ProblemAttachment.objects.filter(
            problem_id=fresh.pk
        ).order_by("order", "id")
    ]
    profile = snapshot_problem_profile(snapshot)
    if profile.submission == "output" or profile.metric:
        attachments = list(
            ProblemAttachment.objects.filter(problem_id=fresh.pk).order_by(
                "order", "id"
            )
        )
        descriptions = []
        total = 0
        for attachment, item in zip(attachments, snapshot["attachments"]):
            key = "dataset:" + attachment.filename
            if key in artifacts:
                raise SnapshotError(
                    _("Dataset attachments must have distinct filenames for review.")
                )
            artifact = _manifest_artifact(attachment.file, persist, include_execution)
            artifacts[key] = artifact
            item.update(artifact=artifact, review_support="dataset")
            descriptions.append(
                {"filename": attachment.filename, "description": attachment.description}
            )
            if artifact:
                total += artifact.get("size") or 0
            if include_execution and artifact and total <= 128 * 1024 * 1024:
                try:
                    with attachment.file.open("rb") as stream:
                        raw = stream.read(1024 * 1024)
                    summary = summarize_data(raw, attachment.filename)
                    if len(raw) != artifact["size"]:
                        summary.update(
                            complete_scan=False,
                            sampled_bytes=len(raw),
                            size=artifact["size"],
                        )
                    item["summary"] = summary
                except (
                    ValueError,
                    OSError,
                    UnicodeError,
                    zipfile.BadZipFile,
                    RuntimeError,
                    RecursionError,
                    SyntaxError,
                ):
                    item["summary"] = {
                        "available": False,
                        "limitation": "Dataset schema could not be safely read; no semantic validation is established.",
                    }
            elif include_execution:
                item["summary"] = {
                    "available": False,
                    "limitation": "Dataset summary budget exceeded; essential unread portions require explicit assessment.",
                }
        snapshot["dataset_descriptions"] = sorted(descriptions, key=canonical_json)
    if profile.submission == "output":
        snapshot["output_examples"] = _capture_output_examples(
            fresh, persist, include_execution
        )
    snapshot["pdf_statement"] = (
        extract_pdf_statement(fresh.pdf_description)
        if include_execution and extract_pdf and artifacts.get("pdf_description")
        else {}
    )
    has_visual_source = bool(
        re.search(
            r"!\[[^\]]*\]\(|<(?:img|svg|canvas|iframe|object|embed|video)\b",
            snapshot["statement"],
            re.IGNORECASE,
        )
        or snapshot["pdf_statement"].get("requires_manual_review")
    )
    snapshot["media_limitations"] = (
        [
            "Text-only review cannot inspect statement images or PDF visuals; essential visual content remains manual, no arbitrary URLs fetched."
        ]
        if has_visual_source
        else []
    )
    snapshot["content_hash_version"] = 3
    snapshot["content_hash"] = digest_json(problem_review_content(snapshot))
    snapshot["package_hash"] = digest_json(execution_package_identity(snapshot))
    snapshot["input_hash"] = problem_review_input_hash(snapshot)
    return snapshot


def _capture_output_examples(problem, persist, include_execution):
    submissions = list(
        Submission.objects.filter(
            problem_id=problem.pk,
            language__key="OUTPUT",
            review_evidence__output_portfolio=True,
            status="D",
        )
        .exclude(result__in=("IE", "AB"))
        .select_related("source")
        .order_by("-pk")[:12]
    )
    cases = {}
    if include_execution:
        for row in (
            SubmissionTestCase.objects.filter(
                submission_id__in=[sub.pk for sub in submissions]
            )
            .order_by("submission_id", "case")
            .values()
        ):
            cases.setdefault(row["submission_id"], []).append(row)
    examples = []
    for index, submission in enumerate(submissions, 1):
        archive_summary = {}
        try:
            source = submission.source.source
            with open_output_source(source) as stream:
                artifact = freeze_file(
                    stream, persist, max_bytes=MAX_OUTPUT_EXAMPLE_BYTES
                )
            if include_execution:
                with open_output_source(source) as stream:
                    raw = stream.read(MAX_OUTPUT_EXAMPLE_BYTES + 1)
                if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                    raise SnapshotError(
                        _(
                            "An output example changed during snapshot capture. Try again."
                        )
                    )
                try:
                    archive_summary = {
                        "valid": True,
                        **summarize_data(raw, "outputs.zip"),
                    }
                except UnsupportedArchiveCompression:
                    archive_summary = {
                        "available": False,
                        "limitation": "The output archive uses compression unsupported by the bounded reader; no member contents were inspected. Manual inspection is needed, not another identical submission.",
                    }
                except (ValueError, OSError, zipfile.BadZipFile, RuntimeError):
                    archive_summary = {
                        "valid": False,
                        "limitation": "The uploaded output archive is malformed, ambiguous or exceeds safe inspection limits; it cannot be treated as a valid positive example.",
                    }
        except (OSError, ValueError, SubmissionSource.DoesNotExist):
            # Missing/unregistered uploads cannot establish prepared evidence.
            continue
        evidence = _scalars(submission) if include_execution else None
        if evidence is not None:
            evidence.update(
                source_hash=hashlib.sha256(source.encode()).hexdigest(),
                cases=cases.get(submission.pk, []),
            )
        examples.append(
            {
                "ref": "output:%s" % index,
                "submission_id": submission.pk,
                "artifact": artifact,
                "source_hash": hashlib.sha256(source.encode()).hexdigest(),
                "archive_summary": archive_summary,
                "evidence": evidence,
            }
        )
    return examples


def build_problem_snapshot(problem, *, include_statement_sources=True):
    snapshot = _capture_problem(
        problem, persist=True, extract_pdf=include_statement_sources
    )
    verification = _capture_problem(problem, persist=False, extract_pdf=False)
    if snapshot["input_hash"] != verification["input_hash"]:
        raise SnapshotError(
            _(
                "The problem changed while its review snapshot was being captured. Try again."
            )
        )
    snapshot["captured_at"] = timezone.now().isoformat()
    return snapshot


def hash_current_problem(problem):
    return _capture_problem(problem, persist=False, include_execution=False)[
        "content_hash"
    ]


def attachment_content_identity(attachments):
    return [
        {
            **{key: value for key, value in item.items() if key != "artifact"},
            "artifact": _artifact_identity(item.get("artifact")),
        }
        for item in attachments
    ]


def problem_package_hash(problem):
    return _capture_problem(problem, persist=False, include_execution=False)[
        "package_hash"
    ]


def build_problem_manifest(problem, include_execution=False):
    """Provenance helper; does not write private snapshot artifacts."""
    snapshot = _capture_problem(
        problem, persist=False, include_execution=include_execution, extract_pdf=False
    )
    if not include_execution:
        snapshot.pop("solutions", None)
        snapshot.pop("validation", None)
    return snapshot


def current_content_hash(obj, problem_hashes=None):
    if isinstance(obj, Problem):
        return hash_current_problem(obj)
    problems = list(
        obj.contest_problems.filter(problem__isnull=False)
        .select_related("problem")
        .order_by("order", "id")
    )
    memo = problem_hashes if problem_hashes is not None else {}
    children = []
    for item in problems:
        if item.problem_id not in memo:
            memo[item.problem_id] = hash_current_problem(item.problem)
        children.append(
            {"problem_id": item.problem_id, "content_hash": memo[item.problem_id]}
        )
    return build_contest_snapshot(obj, children)["content_hash"]


def _bound_evidence(evidence, snapshot, source_hash=None):
    binding = evidence.get("review_evidence") or evidence.get("evidence_snapshot") or {}
    if binding.get("package_hash") != snapshot.get("package_hash"):
        if "date" not in snapshot.get("config", {}):
            return False
        # Older provenance included the publication date. Accept its exact old
        # encoding only when ALL execution inputs still match. A first form Save
        # fills an unset date, so also check the original null-date encoding.
        legacy_hashes = set()
        for identity in (
            legacy_execution_package_identity(snapshot),
            execution_package_identity(snapshot),
        ):
            legacy_hashes.add(digest_json(identity))
            for publication_date in (snapshot.get("config", {}).get("date"), None):
                identity["config"]["date"] = publication_date
                legacy_hashes.add(digest_json(identity))
        if binding.get("package_hash") not in legacy_hashes:
            return False
    if source_hash and binding.get("source_hash") != source_hash:
        return False
    expected = sum(row.get("type") == "C" for row in snapshot.get("tests", []))
    return bool(
        binding.get("environment") and binding.get("expected_cases") == expected > 0
    )


def case_input_manifest(evidence):
    """Actual executed bytes, not generator-source equality or claimed filenames."""
    binding = evidence.get("review_evidence") or evidence.get("evidence_snapshot") or {}
    rows = binding.get("worker_attestation", {}).get("case_manifest", [])
    return [
        (row.get("batch") or 0, row.get("case"), row.get("input_sha256"))
        for row in rows
    ]


def preparation_errors(snapshot):
    errors = []
    if snapshot.get("kind") == "contest":
        if not snapshot.get("items"):
            errors.append(_("The contest has no items."))
        config = snapshot.get("config", {})
        try:
            start = datetime.fromisoformat(config.get("start_time", ""))
            end = datetime.fromisoformat(config.get("end_time", ""))
            if end <= start:
                errors.append(_("The contest end must be after its start."))
            duration = (end - start).total_seconds()
            if (
                config.get("time_limit") is not None
                and not 0 < config["time_limit"] <= duration
            ):
                errors.append(
                    _("Contest duration must be positive and fit its window.")
                )
            if (
                config.get("freeze_after") is not None
                and not 0 <= config["freeze_after"] <= duration
            ):
                errors.append(_("The scoreboard freeze must fit the contest window."))
            formats[config.get("format_name")].validate(config.get("format_config", {}))
        except (TypeError, ValueError, KeyError, ValidationError):
            errors.append(
                _("The contest timing, format, or format configuration is invalid.")
            )
        if any(
            (not item.get("problem_id") and not item.get("quiz_id"))
            or item.get("problem_id")
            and item.get("quiz_id")
            or item.get("points", -1) < 0
            for item in snapshot.get("items", [])
        ):
            errors.append(_("Contest item references and points must be valid."))
        return errors
    if not snapshot.get("statement", "").strip() and not snapshot.get(
        "artifacts", {}
    ).get("pdf_description"):
        errors.append(_("A readable problem statement is required."))
    data = snapshot.get("data_config", {})
    artifacts = snapshot.get("artifacts", {})
    profile = snapshot_problem_profile(snapshot)
    if profile.issues:
        errors.append(
            _(
                "The judging configuration is invalid or conflicts with the saved problem settings. Check Test Data before reviewing."
            )
        )
    elif not profile.review_supported:
        errors.append(
            _(
                "This problem type is not supported by coordinator review yet; ask a superuser for manual review."
            )
        )
    for component in mismatched_judging_components(snapshot):
        label = {
            "checker": _("Checker"),
            "interactor": _("Interactor"),
            "manager": _("Manager"),
            "signature": _("Function / IOI signature"),
        }[component]
        errors.append(
            _(
                "The %(component)s configuration in init.yml does not match Test Data. Save Test Data and run preparation again."
            )
            % {"component": label}
        )
    if profile.signature:
        runtimes = snapshot.get("allowed_language_keys", [])
        families = enabled_signature_families(runtimes)
        rows = [
            row for row in snapshot.get("signature_graders", []) if row.get("active")
        ]
        if (
            not families
            or any(signature_family(key) is None for key in runtimes)
            or any(
                len([row for row in rows if row.get("family") == family]) != 1
                or not artifacts.get("signature:%s:handler" % family)
                or family == "c"
                and not artifacts.get("signature:c:header")
                for family in families
            )
        ):
            errors.append(
                _(
                    "Each enabled signature language family needs exactly one grader and its required interface files."
                )
            )
    if profile.execution == "interactive" and not artifacts.get("interactive_judge"):
        errors.append(
            _("An interactor source is required for this interactive problem.")
        )
    if profile.execution == "communication" and not artifacts.get(
        "communication_manager"
    ):
        errors.append(_("A manager source is required for this communication problem."))
    tests = [row for row in snapshot.get("tests", []) if row.get("type") == "C"]
    if not tests or not (artifacts.get("zipfile") or artifacts.get("generator")):
        errors.append(_("A usable nonempty official testcase package is required."))
    if snapshot.get("inventory", {}).get("errors"):
        errors.append(
            _(
                "The testcase archive has missing or ambiguous configured input/output files."
            )
        )
    if not data.get("checker"):
        errors.append(_("A checker must be configured."))
    checker_names = {row["checker"] for row in checker_configurations(snapshot)} | {
        data.get("checker")
    }
    if "custom" in checker_names and not artifacts.get("custom_checker"):
        errors.append(_("The configured custom checker source is required."))
    elif (
        checker_names.intersection(("customcpp", "testlib", "testlibcms"))
        and not artifacts.get("custom_checker_cpp")
        and not (
            profile.execution == "communication"
            and checker_names.intersection(("customcpp", "testlib", "testlibcms"))
            == {"testlibcms"}
        )
    ):
        errors.append(_("The configured C++ checker source is required."))
    requires_validator = profile.submission != "output" or bool(
        artifacts.get("testcase_validator")
    )
    if requires_validator and not artifacts.get("testcase_validator"):
        errors.append(_("A validator source is required."))
    validation = snapshot.get("validation") or {}
    validator_hash = (artifacts.get("testcase_validator") or {}).get("sha256")
    if requires_validator and not (
        validation.get("status") == "D"
        and validation.get("passed") is True
        and validation.get("total_cases") == len(tests) > 0
        and len(validation.get("cases", [])) == len(tests)
        and all(
            row.get("status") in ("AC", "PASS", "OK")
            for row in validation.get("cases", [])
        )
        and _bound_evidence(validation, snapshot, validator_hash)
    ):
        errors.append(
            _("Run Validation again and make sure every official input passes.")
        )
    if profile.submission == "output":
        fresh_examples = []
        for example in snapshot.get("output_examples", []):
            evidence = example.get("evidence") or {}
            binding = evidence.get("review_evidence") or {}
            artifact_hash = (example.get("artifact") or {}).get("sha256")
            if (
                evidence.get("status") == "D"
                and not evidence.get("is_pretested")
                and evidence.get("result") in ("AC", "WA")
                and example.get("archive_summary", {}).get("valid") is not False
                and len(evidence.get("cases", [])) == len(tests) > 0
                and all(
                    row.get("status") not in (None, "SC", "IE", "AB")
                    for row in evidence.get("cases", [])
                )
                and _bound_evidence(evidence, snapshot, example.get("source_hash"))
                and artifact_hash
                and binding.get("output_artifact_sha256") == artifact_hash
                and binding.get("worker_attestation", {}).get("output_artifact_sha256")
                == artifact_hash
            ):
                fresh_examples.append(example)
        if not fresh_examples:
            errors.append(
                _(
                    "Upload and judge at least one current output example through Submit before reviewing. A perfect score is not required."
                )
            )
        return errors
    solutions = snapshot.get("solutions", [])
    full_ac = False
    validation_manifest = case_input_manifest(validation)
    if not solutions:
        errors.append(
            _("Add and run Solution Codes, including a full accepted solution.")
        )
    for code in solutions:
        evidence = code.get("evidence") or {}
        source_hash = hashlib.sha256(code.get("source", "").encode()).hexdigest()
        fresh = (
            evidence.get("status") in ("D", "CE")
            and evidence.get("result") not in (None, "IE", "AB")
            and evidence.get("source_hash") == source_hash
            and _bound_evidence(evidence, snapshot, source_hash)
        )
        if not fresh:
            errors.append(
                _(
                    "Run Solution Code “%(name)s” again; its result is missing, unfinished, or out of date."
                )
                % {"name": code.get("name") or code["ref"]}
            )
        cases = evidence.get("cases", [])
        if (
            profile.metric
            and fresh
            and evidence.get("status") == "D"
            and not evidence.get("is_pretested")
            and len(cases) == len(tests) > 0
            and all(row.get("status") not in ("SC", "IE", "AB", None) for row in cases)
        ):
            full_ac = True
        if (
            fresh
            and code.get("expected_result") == "AC"
            and evidence.get("result") == "AC"
            and not evidence.get("is_pretested")
            and len(cases) == len(tests) > 0
            and all(
                row.get("status") == "AC" and row.get("points") == row.get("total")
                for row in cases
            )
            and evidence.get("case_total", 0) > 0
            and evidence.get("case_points") == evidence.get("case_total")
        ):
            solution_manifest = case_input_manifest(evidence)
            if (
                len(validation_manifest) == len(tests)
                and validation_manifest == solution_manifest
                and all(row[2] for row in validation_manifest)
            ):
                full_ac = True
            else:
                errors.append(
                    _(
                        "Validation and the full accepted solution must attest to identical actual official input bytes."
                    )
                )
    if not full_ac and profile.metric:
        errors.append(
            _(
                "Run at least one current scoring submission on every official testcase. A perfect score is not required."
            )
        )
    elif not full_ac:
        errors.append(
            _(
                "At least one full accepted solution must pass every official test with current execution results."
            )
        )
    return errors


def build_contest_snapshot(contest, children):
    # Root binds selected/attached child runs before calling this function.
    config = _scalars(contest, ("id", "access_code", "is_locked"))
    config["has_access_code"] = bool(contest.access_code)
    items = list(contest.contest_problems.order_by("order", "id").values())
    roles = {
        name: sorted(getattr(contest, name).values_list("pk", flat=True))
        for name in ("authors", "curators", "testers", "organizations")
    }
    quiz_ids = [item["quiz_id"] for item in items if item.get("quiz_id")]
    quizzes = list(Quiz.objects.filter(pk__in=quiz_ids).order_by("id"))
    assignments = list(
        QuizQuestionAssignment.objects.filter(quiz_id__in=quiz_ids)
        .order_by("quiz_id", "order", "id")
        .values()
    )
    question_ids = [assignment["question_id"] for assignment in assignments]
    quiz_content = {
        "quizzes": [
            _scalars(quiz, ("is_locked", "created_at", "is_public")) for quiz in quizzes
        ],
        "assignments": assignments,
        "questions": list(
            QuizQuestion.objects.filter(pk__in=question_ids)
            .order_by("id")
            .values(
                "id",
                "question_type",
                "title",
                "content",
                "choices",
                "correct_answers",
                "explanation",
                "shuffle_choices",
                "grading_strategy",
                "multiple_true_false_score_table",
            )
        ),
    }
    exposure = _contest_exposure(contest, items, roles)
    snapshot = {
        "kind": "contest",
        "policy_version": POLICY_VERSION,
        "contest_id": contest.pk,
        "config": config,
        "items": items,
        "children": children,
        "roles": roles,
        "quiz_content": quiz_content,
        "exposure": exposure,
    }
    snapshot["content_hash_version"] = 3
    snapshot["content_hash"] = digest_json(contest_review_content(snapshot))
    snapshot["input_hash"] = digest_json(snapshot)
    snapshot["captured_at"] = timezone.now().isoformat()
    return snapshot


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


def _contest_exposure(contest, items, roles, *, detail_limit=0):
    trusted = set(roles["authors"] + roles["curators"] + roles["testers"])
    problem_ids = [item["problem_id"] for item in items if item.get("problem_id")]
    quiz_ids = [item["quiz_id"] for item in items if item.get("quiz_id")]
    problems = list(
        Problem.objects.filter(pk__in=problem_ids).values(
            "id", "is_public", "is_organization_private"
        )
    )
    quizzes = list(Quiz.objects.filter(pk__in=quiz_ids).values("id", "is_public"))
    private_problems = [
        problem["id"]
        for problem in problems
        if not problem["is_public"] or problem["is_organization_private"]
    ]
    private_quizzes = [quiz["id"] for quiz in quizzes if not quiz["is_public"]]
    released = bool(
        contest.is_visible
        and not contest.is_private
        and not contest.is_organization_private
        and not contest.is_in_course
        and contest.start_time
        and contest.start_time <= timezone.now()
    )
    submissions = (
        Submission.objects.filter(problem_id__in=private_problems)
        .exclude(user_id__in=trusted)
        .exclude(user__user__is_superuser=True)
    )
    attempts = (
        QuizAttempt.objects.filter(quiz_id__in=private_quizzes)
        .exclude(user_id__in=trusted)
        .exclude(user__user__is_superuser=True)
    )
    # Ordinary post-release interaction is not private-content leakage. Before a
    # public scheduled contest starts, its problem set can still be confidential.
    if released:
        submissions = submissions.filter(date__lt=contest.start_time)
        attempts = attempts.filter(start_time__lt=contest.start_time)
    sub_counts = list(
        submissions.values("problem_id")
        .annotate(count=Count("id"), last_id=Max("id"), last_date=Max("date"))
        .order_by("problem_id")
    )
    attempt_counts = list(
        attempts.values("quiz_id")
        .annotate(count=Count("id"), last_id=Max("id"), last_date=Max("start_time"))
        .order_by("quiz_id")
    )
    capability = []
    if not released:
        for model, ids, target in (
            (Problem, private_problems, "problem"),
            (Quiz, private_quizzes, "quiz"),
        ):
            for role in ("authors", "curators", "testers"):
                relation = getattr(model, role).through
                queryset = (
                    relation.objects.filter(**{target + "_id__in": ids})
                    .exclude(profile_id__in=trusted)
                    .exclude(profile__user__is_superuser=True)
                )
                # Full identity evidence is retained; no per-profile query loop.
                capability.extend(
                    {
                        "kind": target,
                        "role": role,
                        "item_id": item_id,
                        "profile_id": profile_id,
                    }
                    for item_id, profile_id in queryset.values_list(
                        target + "_id", "profile_id"
                    ).order_by(target + "_id", "profile_id")
                )
    evidence = {
        "trusted_profile_ids": sorted(trusted),
        "submissions": [
            {key: _json_value(value) for key, value in row.items()}
            for row in sub_counts
        ],
        "quiz_attempts": [
            {key: _json_value(value) for key, value in row.items()}
            for row in attempt_counts
        ],
        "access_capabilities": capability,
        "capability_is_not_proof_of_reading": True,
        "public_problem_ids": [
            problem["id"]
            for problem in problems
            if problem["is_public"] and not problem["is_organization_private"]
        ],
        "material_concern": bool(sub_counts or attempt_counts or capability),
        "release_context": "public and started" if released else "not yet released",
        "limitations": [
            "No complete historical view/access log; role grants show capability, not confirmed viewing."
        ],
    }
    if detail_limit:
        # Reader-facing details use the same filtered querysets as the live
        # publication gate. They are not added to AI snapshots by default.
        # Aggregate ALL matching activity before limiting individual evidence.
        evidence["submission_groups"] = list(
            submissions.order_by()
            .values("user_id", "problem_id")
            .annotate(count=Count("id"), last_date=Max("date"))
        )
        evidence["quiz_attempt_groups"] = list(
            attempts.order_by()
            .values("user_id", "quiz_id")
            .annotate(count=Count("id"), last_date=Max("start_time"))
        )
        evidence["submission_details"] = list(
            submissions.annotate(
                access_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("user_id"), F("problem_id")],
                    order_by=[F("date").desc(), F("id").desc()],
                )
            )
            .filter(access_rank__lte=detail_limit)
            .order_by("-date", "-id")
            .values("id", "user_id", "problem_id", "date", "contest_object_id")
        )
        evidence["quiz_attempt_details"] = list(
            attempts.annotate(
                access_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("user_id"), F("quiz_id")],
                    order_by=[F("start_time").desc(), F("id").desc()],
                )
            )
            .filter(access_rank__lte=detail_limit)
            .order_by("-start_time", "-id")
            .values("id", "user_id", "quiz_id", "start_time")
        )
    return evidence
