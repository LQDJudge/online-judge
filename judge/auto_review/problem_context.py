"""Collect current problem data for review."""

import hashlib
import os
import re
import zipfile
from collections import defaultdict
from threading import local

from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils.translation import gettext as _

from judge.auto_review.artifact_readers import (
    MAX_OUTPUT_EXAMPLE_BYTES,
    SAFE_ZIP_COMPRESSION,
    UnsupportedArchiveCompression,
    open_output_source,
    summarize_data,
)
from judge.auto_review.context_utils import (
    ReviewInputError,
    _scalars,
    canonical_json,
    digest_json,
)
from judge.auto_review.execution_freshness import (
    solution_execution_hash,
    solution_package_identity,
    validation_execution_hash,
)
from judge.auto_review.pdf_statement import extract_pdf_statement
from judge.auto_review.problem_types import (
    problem_profile_from_context,
    resolve_problem_profile,
)
from judge.auto_review.profile_config import (
    MAX_PROFILE_CONFIG_BYTES,
    ProfileConfigError,
    parse_profile_config,
)
from judge.auto_review.review_hash import (
    problem_review_content,
    problem_review_input_hash,
)
from judge.auto_review.signature import (
    enabled_signature_families,
    relevant_signature_config,
)
from judge.models import (
    Problem,
    ProblemAttachment,
    Submission,
    SubmissionSource,
    SubmissionTestCase,
)
from judge.models.problem import (
    LanguageLimit,
    LanguageTemplate,
    ProblemTranslation,
    Solution,
)
from judge.models.problem_data import (
    ProblemData,
    ProblemSignatureGrader,
    ProblemSolutionCode,
    ProblemTestCase,
    ProblemValidation,
    problem_data_storage,
)

_input_revision = local()

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

MAX_INVENTORY_PREVIEW_CHARS = 64 * 1024


def describe_file(field, *, max_bytes=None, storage=None):
    """Describe an existing upload; never copy files into a review archive."""
    if not field or not field.name:
        return None
    storage = storage or getattr(field, "storage", problem_data_storage)
    name = field.name
    if os.path.isabs(name):
        root = os.path.realpath(storage.location)
        path = os.path.realpath(name)
        if os.path.commonpath([root, path]) != root:
            raise ReviewInputError(_("Invalid problem file reference."))
        name = os.path.relpath(path, root)
    if ".." in name.split("/") or os.path.isabs(name):
        raise ReviewInputError(_("Invalid problem file reference."))
    size = storage.size(name)
    if max_bytes is not None and size > max_bytes:
        raise ReviewInputError(_("Problem file exceeds this operation's input limit."))
    return {
        "backend": ("problem-data" if storage is problem_data_storage else "default"),
        "storage_name": name,
        "filename": os.path.basename(name),
        "size": size,
        # Compatibility identity for source-reading caches, not execution proof.
        "revision": digest_json(
            {"name": name, "revision": getattr(_input_revision, "value", {})}
        ),
    }


def read_artifact(artifact, max_bytes=None):
    """Bounded read from existing storage; caller has already authorized the object."""
    name = artifact.get("storage_name", "")
    if not name or os.path.isabs(name) or ".." in name.split("/"):
        raise ReviewInputError(_("Invalid problem file reference."))
    backend = artifact.get("backend")
    if backend not in ("problem-data", "default"):
        raise ReviewInputError(_("Invalid problem file reference."))
    storage = problem_data_storage if backend == "problem-data" else default_storage
    limit = max_bytes if max_bytes is not None else MAX_ARTIFACT_BYTES
    with storage.open(name, "rb") as stream:
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ReviewInputError(_("Problem file exceeds this operation's input limit."))
    return result


def _zip_inventory(field, tests):
    if not field:
        return {
            "kind": "generator",
            "configured_cases": sum(row["type"] == "C" for row in tests),
            "limitations": [
                "Generator source and current judging outcomes are available; extracted per-test statistics do not apply."
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
            preview_budget = MAX_INVENTORY_PREVIEW_CHARS
            omitted_previews = 0
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
                    if preview_budget <= 0:
                        omitted_previews += 1
                        continue
                    with archive.open(entry) as case_stream:
                        preview = case_stream.read(4097)
                    if len(preview) <= 4096:
                        key = kind + "_full_small"
                        value = preview.decode("utf8", "replace")
                    else:
                        key = kind + "_preview"
                        value = preview[:512].decode("utf8", "replace")
                    cost = len(canonical_json({key: value}))
                    if cost > preview_budget:
                        omitted_previews += 1
                    else:
                        record[key] = value
                        preview_budget -= cost
                records.append(record)
            return {
                "kind": "zip",
                "configured_cases": len(records),
                "zip_members": len(entries),
                "records": records,
                "errors": errors,
                "limitations": [
                    "Large-file previews are not complete coverage statistics; dimensions require validated parsing."
                ]
                + (
                    [
                        "Some test previews were omitted to keep the review input bounded."
                    ]
                    if omitted_previews
                    else []
                ),
            }
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReviewInputError(
            _("The testcase archive is unreadable or corrupt.")
        ) from exc


def _read_auxiliary_sources(data, code, strict=True, extra_roots=()):
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
                    artifacts["aux:" + name] = describe_file(stream)
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


def _manifest_artifact(field, strict=True):
    try:
        return describe_file(field)
    except (OSError, ReviewInputError):
        if strict:
            raise
        return {
            "filename": os.path.basename(field.name),
            "sha256": None,
            "size": None,
            "unavailable": True,
            "reason": "missing_or_unreadable",
        }


def _capture_problem(
    problem,
    include_execution=True,
    extract_pdf=True,
    identity_only=False,
    batch=None,
):
    # This is an integrity boundary: read actual rows, not possibly stale cached
    # objects. Queries are batched, including the portfolio's per-case evidence.
    fresh = (
        batch["problems"][problem.pk]
        if batch is not None
        else Problem.objects.get(pk=problem.pk)
    )
    data = (
        batch["data"].get(fresh.pk)
        if batch is not None
        else ProblemData.objects.filter(problem_id=fresh.pk).first()
    )
    _input_revision.value = {
        key: getattr(data, key, 0)
        for key in ("test_revision", "validator_revision", "judging_revision")
    }
    _input_revision.value["files"] = getattr(fresh, "review_files_revision", 0)
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
            name: _manifest_artifact(getattr(data, name), include_execution)
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
            artifacts["init_yml"] = describe_file(init_file)
        try:
            effective_config = parse_profile_config(raw_config)
        except ProfileConfigError:
            # Preserve the artifact and diagnose it in preparation, never treat
            # an unreadable effective mode as ordinary batch.
            effective_config = []
    artifacts["pdf_description"] = _manifest_artifact(
        fresh.pdf_description, include_execution
    )
    runtime_keys = (
        batch["language_keys"][fresh.pk]
        if batch is not None
        else list(fresh.allowed_languages.order_by("key").values_list("key", flat=True))
    )
    signature_init_identity = None
    if data and data.use_ioi_signature and isinstance(effective_config, dict):
        try:
            signature_init_identity = {
                "configuration_sha256": digest_json(
                    relevant_signature_config(effective_config, runtime_keys)
                ),
            }
        except (TypeError, ValueError):
            raise ReviewInputError(
                _("The signature judge configuration contains unsupported values.")
            )
    signature_graders = []
    signature_roots = []
    if data and data.use_ioi_signature:
        enabled_families = enabled_signature_families(runtime_keys)
        graders = (
            batch["graders"][fresh.pk]
            if batch is not None
            else ProblemSignatureGrader.objects.filter(problem_id=fresh.pk).order_by(
                "language", "pk"
            )
        )
        for grader in graders:
            active = grader.language in enabled_families
            entry = {"family": grader.language, "active": active}
            if active:
                for component in ("handler", "header"):
                    field = getattr(grader, component)
                    key = "signature:%s:%s" % (grader.language, component)
                    artifacts[key] = _manifest_artifact(field, include_execution)
                    entry[component] = key if artifacts[key] else None
                    if field:
                        signature_roots.append(field)
            signature_graders.append(entry)
    auxiliary, auxiliary_limits = (
        ({}, [])
        if identity_only
        else _read_auxiliary_sources(
            data,
            fresh.code,
            strict=include_execution,
            extra_roots=signature_roots,
        )
    )
    artifacts.update(auxiliary)
    tests = (
        [dict(row) for row in batch["tests"][fresh.pk]]
        if batch is not None
        else list(
            ProblemTestCase.objects.filter(dataset_id=fresh.pk)
            .order_by("order", "id")
            .values()
        )
    )
    for row in tests:
        row.pop("dataset_id", None)
    codes = (
        batch["codes"][fresh.pk]
        if batch is not None
        else list(
            ProblemSolutionCode.objects.filter(problem_id=fresh.pk)
            .select_related("language", "last_submission")
            .order_by("order", "id")
        )
    )
    submission_ids = (
        [code.last_submission_id for code in codes if code.last_submission_id]
        if include_execution
        else []
    )
    sources = (
        batch["sources"]
        if batch is not None
        else dict(
            SubmissionSource.objects.filter(
                submission_id__in=submission_ids
            ).values_list("submission_id", "source")
        )
    )
    case_map = {sid: [] for sid in submission_ids}
    for result in (
        (row for sid in submission_ids for row in batch["cases"][sid])
        if batch is not None
        else SubmissionTestCase.objects.filter(submission_id__in=submission_ids)
        .order_by("submission_id", "case")
        .values()
    ):
        case_map[result["submission_id"]].append(result)
    solutions = []
    package_identity = (
        _batch_solution_identity(fresh, batch, tests)
        if batch is not None and include_execution and codes
        else solution_package_identity(fresh) if include_execution and codes else None
    )
    for index, code in enumerate(codes):
        submission = code.last_submission if include_execution else None
        evidence = _scalars(submission) if submission else None
        if evidence:
            evidence["current_execution_hash"] = solution_execution_hash(
                fresh, code.source_code, code.language_id, package=package_identity
            )
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
            batch["validations"].get(fresh.pk)
            if batch is not None
            else ProblemValidation.objects.filter(problem_id=fresh.pk)
            .prefetch_related("results")
            .order_by("-date", "-id")
            .first()
        )
        if include_execution
        else None
    )
    validation_evidence = _scalars(validation) if validation else None
    if validation:
        validation_evidence["current_execution_hash"] = (
            validation_execution_hash(
                fresh,
                data=batch["data_values"].get(fresh.pk, {}),
                tests=[
                    {
                        key: row[key]
                        for key in ("order", "type", "input_file", "generator_args")
                    }
                    for row in tests
                ],
            )
            if batch is not None
            else validation_execution_hash(fresh)
        )
        validation_evidence["cases"] = [
            _scalars(result) for result in validation.results.all()
        ]
    editorial = (
        batch["editorials"].get(fresh.pk)
        if batch is not None
        else Solution.objects.filter(problem_id=fresh.pk)
        .values("content", "is_public")
        .first()
    )
    limits = (
        batch["limits"][fresh.pk]
        if batch is not None
        else list(
            LanguageLimit.objects.filter(problem_id=fresh.pk)
            .order_by("language_id")
            .values()
        )
    )
    templates = [
        {"language": row["language__key"], "source": row["source"]}
        for row in (
            batch["templates"][fresh.pk]
            if batch is not None
            else LanguageTemplate.objects.filter(problem_id=fresh.pk)
            .order_by("language__key")
            .values("language__key", "source")
        )
    ]
    role_names = (
        ("allowed_languages",)
        if identity_only
        else ("authors", "curators", "testers", "allowed_languages", "organizations")
    )
    roles = {
        name: (
            batch["language_ids"][fresh.pk]
            if batch is not None
            else sorted(getattr(fresh, name).values_list("pk", flat=True))
        )
        for name in role_names
    }
    data_config = _scalars(data, ("id", "problem", "feedback")) if data else {}
    snapshot = {
        "kind": "problem",
        "file_revisions": dict(_input_revision.value),
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
        if include_execution and not identity_only
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
    snapshot["translations"] = (
        batch["translations"][fresh.pk]
        if batch is not None
        else list(
            ProblemTranslation.objects.filter(problem_id=fresh.pk)
            .order_by("language", "id")
            .values("language", "name", "description")
        )
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
        for attachment in (
            batch["attachments"][fresh.pk]
            if batch is not None
            else ProblemAttachment.objects.filter(problem_id=fresh.pk).order_by(
                "order", "id"
            )
        )
    ]
    profile = problem_profile_from_context(snapshot)
    if profile.submission == "output" or profile.metric:
        attachments = (
            batch["attachments"][fresh.pk]
            if batch is not None
            else list(
                ProblemAttachment.objects.filter(problem_id=fresh.pk).order_by(
                    "order", "id"
                )
            )
        )
        descriptions = []
        total = 0
        for attachment, item in zip(attachments, snapshot["attachments"]):
            key = "dataset:" + attachment.filename
            if key in artifacts:
                raise ReviewInputError(
                    _("Dataset attachments must have distinct filenames for review.")
                )
            artifact = _manifest_artifact(attachment.file, include_execution)
            artifacts[key] = artifact
            item.update(artifact=artifact, review_support="dataset")
            descriptions.append(
                {"filename": attachment.filename, "description": attachment.description}
            )
            if artifact:
                total += artifact.get("size") or 0
            if (
                include_execution
                and not identity_only
                and artifact
                and total <= 128 * 1024 * 1024
            ):
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
            fresh,
            include_execution,
            inspect_archive=not identity_only,
            batch=batch,
            tests=tests,
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
    snapshot["content_hash"] = digest_json(problem_review_content(snapshot))
    snapshot["input_hash"] = problem_review_input_hash(snapshot)
    return snapshot


def _capture_output_examples(
    problem, include_execution, inspect_archive=True, *, batch=None, tests=None
):
    submissions = (
        batch["outputs"][problem.pk]
        if batch is not None
        else list(
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
    )
    cases = batch["cases"] if batch is not None else {}
    if include_execution and batch is None:
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
                artifact = describe_file(
                    stream,
                    max_bytes=MAX_OUTPUT_EXAMPLE_BYTES,
                    storage=default_storage,
                )
            if include_execution and inspect_archive:
                with open_output_source(source) as stream:
                    raw = stream.read(MAX_OUTPUT_EXAMPLE_BYTES + 1)
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
            evidence["current_execution_hash"] = solution_execution_hash(
                problem,
                source,
                submission.language_id,
                package=(
                    _batch_solution_identity(problem, batch, tests)
                    if batch is not None
                    else None
                ),
            )
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


def build_problem_context(problem, *, include_statement_sources=True):
    return _capture_problem(problem, extract_pdf=include_statement_sources)


def _batch_problem_identities(problem_ids):
    """Read the current rows for all contest problems in bounded query groups."""
    problem_ids = set(problem_ids)
    # CacheableModel initialization may consult its cache; warm it in one batch.
    # The identities below still read fresh model rows from the database.
    Problem.get_cached_instances(*problem_ids)
    batch = {
        "problems": Problem.objects.in_bulk(problem_ids),
        "data": {
            row.problem_id: row
            for row in ProblemData.objects.filter(problem_id__in=problem_ids)
        },
        "data_values": {
            row["problem_id"]: row
            for row in ProblemData.objects.filter(problem_id__in=problem_ids).values()
        },
        "language_keys": defaultdict(list),
        "language_ids": defaultdict(list),
        "graders": defaultdict(list),
        "tests": defaultdict(list),
        "codes": defaultdict(list),
        "cases": defaultdict(list),
        "limits": defaultdict(list),
        "templates": defaultdict(list),
        "translations": defaultdict(list),
        "attachments": defaultdict(list),
        "outputs": defaultdict(list),
    }
    through = Problem.allowed_languages.through
    for pid, lid, key in (
        through.objects.filter(problem_id__in=problem_ids)
        .order_by("language__key")
        .values_list("problem_id", "language_id", "language__key")
    ):
        batch["language_keys"][pid].append(key)
        batch["language_ids"][pid].append(lid)
    for ids in batch["language_ids"].values():
        ids.sort()
    for grader in ProblemSignatureGrader.objects.filter(
        problem_id__in=problem_ids
    ).order_by("language", "pk"):
        batch["graders"][grader.problem_id].append(grader)
    for row in (
        ProblemTestCase.objects.filter(dataset_id__in=problem_ids)
        .order_by("order", "id")
        .values()
    ):
        batch["tests"][row["dataset_id"]].append(row)
    submission_ids = []
    for code in (
        ProblemSolutionCode.objects.filter(problem_id__in=problem_ids)
        .select_related("language", "last_submission")
        .order_by("order", "id")
    ):
        batch["codes"][code.problem_id].append(code)
        if code.last_submission_id:
            submission_ids.append(code.last_submission_id)
    for submission in (
        Submission.objects.filter(
            problem_id__in=problem_ids,
            language__key="OUTPUT",
            review_evidence__output_portfolio=True,
            status="D",
        )
        .exclude(result__in=("IE", "AB"))
        .annotate(
            _review_rank=Window(
                expression=RowNumber(),
                partition_by=[F("problem_id")],
                order_by=F("pk").desc(),
            )
        )
        .filter(_review_rank__lte=12)
        .select_related("source")
        .order_by("-pk")
    ):
        batch["outputs"][submission.problem_id].append(submission)
        submission_ids.append(submission.pk)
    batch["sources"] = dict(
        SubmissionSource.objects.filter(submission_id__in=submission_ids).values_list(
            "submission_id", "source"
        )
    )
    for row in (
        SubmissionTestCase.objects.filter(submission_id__in=submission_ids)
        .order_by("submission_id", "case")
        .values()
    ):
        batch["cases"][row["submission_id"]].append(row)
    validations = (
        ProblemValidation.objects.filter(problem_id__in=problem_ids)
        .annotate(
            _review_rank=Window(
                expression=RowNumber(),
                partition_by=[F("problem_id")],
                order_by=[F("date").desc(), F("id").desc()],
            )
        )
        .filter(_review_rank=1)
        .prefetch_related("results")
    )
    batch["validations"] = {row.problem_id: row for row in validations}
    batch["editorials"] = {
        row["problem_id"]: {"content": row["content"], "is_public": row["is_public"]}
        for row in Solution.objects.filter(problem_id__in=problem_ids).values(
            "problem_id", "content", "is_public"
        )
    }
    for row in (
        LanguageLimit.objects.filter(problem_id__in=problem_ids)
        .order_by("language_id")
        .values()
    ):
        batch["limits"][row["problem_id"]].append(row)
    for row in (
        LanguageTemplate.objects.filter(problem_id__in=problem_ids)
        .order_by("language__key")
        .values("problem_id", "language__key", "source")
    ):
        batch["templates"][row["problem_id"]].append(row)
    for row in (
        ProblemTranslation.objects.filter(problem_id__in=problem_ids)
        .order_by("language", "id")
        .values("problem_id", "language", "name", "description")
    ):
        pid = row.pop("problem_id")
        batch["translations"][pid].append(row)
    for attachment in ProblemAttachment.objects.filter(
        problem_id__in=problem_ids
    ).order_by("order", "id"):
        batch["attachments"][attachment.problem_id].append(attachment)
    return batch


def _batch_solution_identity(problem, batch, tests):
    return solution_package_identity(
        problem,
        data=batch["data_values"].get(problem.pk, {}),
        tests=[{k: v for k, v in row.items() if k != "id"} for row in tests],
        language_limits=[
            {k: row[k] for k in ("language_id", "time_limit", "memory_limit")}
            for row in batch["limits"][problem.pk]
        ],
        signature_graders=[
            {
                "language": grader.language,
                "handler": grader.handler.name,
                "header": grader.header.name,
            }
            for grader in sorted(
                batch["graders"][problem.pk], key=lambda grader: grader.pk
            )
        ],
    )


def hash_current_problem(problem, batch=None):
    return _capture_problem(
        problem,
        include_execution=True,
        extract_pdf=False,
        identity_only=True,
        batch=batch,
    )["input_hash"]
