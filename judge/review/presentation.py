"""Readable presentation of persisted model prose; never rewrite review evidence."""

import re

from django.utils.translation import gettext as _
from judge.review.types import snapshot_problem_profile


def publication_request_hint(
    run, blockers=(), *, pending=False, stale=False, earlier=False
):
    """Brief action guidance; does not change publication eligibility."""
    if pending:
        return _(
            "Publication has already been requested. Awaiting an administrator's decision."
        )
    if not run:
        return _("Run a review before requesting publication.")
    if earlier:
        return _("Open the latest review to request publication.")
    if stale:
        return next(
            iter(blockers),
            _("Reviewed content or evidence changed. Run a new review first."),
        )
    if run.status in ("R", "B", "E", "C"):
        return {
            "R": _("Wait for the review to finish."),
            "B": _(
                "Resolve the failed infrastructure checks before requesting publication."
            ),
            "E": _(
                "The review could not finish. Resolve the error and run a review again."
            ),
            "C": _("This review was cancelled. Complete a new review first."),
        }[run.status]
    if run.workflow_version != 2:
        return _("Run a new review before requesting publication.")
    if run.readiness == "NEEDS_AUTHOR_FIX":
        return _("Fix the issues identified in this review, then run it again.")
    if run.readiness == "MANUAL_REVIEW":
        return _("An administrator must resolve the flagged findings first.")
    if run.readiness == "INCOMPLETE":
        return _("Complete the missing checks before requesting publication.")
    return next(
        iter(blockers), _("Check the review findings before requesting publication.")
    )


def _supporting_evidence_unavailable(stage):
    output = stage.output_json if isinstance(stage.output_json, dict) else {}
    if output.get("_missing_evidence"):
        return True
    # Extraction is supplemental: generator-only input bytes may legitimately
    # be unavailable. H3 judges whether that limits its assessment. Actual stage
    # errors and missing upstream evidence remain visible independently.
    if stage.stage_id in ("P_EXTRACT", "EXTRACT_REPAIR", "P_EXTRACT_FINAL"):
        return False
    return (
        output.get("availability") == "unavailable" or output.get("available") is False
    )


def review_section_groups(stages, kind="problem", snapshot=None):
    """Group persisted processing steps without changing their review semantics."""
    definitions = (
        [
            ("D", _("Preparation"), ("D",)),
            ("H1", _("Statement and editorial"), ("E", "H1")),
            ("H2", _("Solutions and correctness"), ("FG", "H2_BLIND", "H2")),
            (
                "H3",
                _("Generator and test coverage"),
                ("P_EXTRACT", "EXTRACT_REPAIR", "P_EXTRACT_FINAL", "H3"),
            ),
            ("H4", _("Validator"), ("H4_PLAN", "P_VALIDATOR", "H4")),
            ("H5", _("Checker"), ("H5_PLAN", "P_CHECKER", "H5")),
            ("H6", _("Limits and difficulty"), ("H6",)),
            ("H7", _("Problem similarity"), ("S", "H7")),
        ]
        if kind == "problem"
        else [
            ("D", _("Preparation"), ("D", "E")),
            ("H1", _("Difficulty and diversity"), ("H1",)),
            ("H2", _("Rules and consistency"), ("H2",)),
        ]
    )
    if kind == "problem" and snapshot:
        profile = snapshot_problem_profile(snapshot)
        titles = {}
        if profile.submission == "output":
            titles.update(
                H2=_("Example submissions and correctness"),
                H4=_("Data validation"),
                H5=_("Scoring"),
            )
        if profile.execution == "interactive":
            titles["H5"] = _("Interactor and checker")
        elif profile.execution == "communication":
            titles["H5"] = _("Manager and checker")
        definitions = [
            (identity, titles.get(identity, title), members)
            for identity, title, members in definitions
        ]
    by_id = {stage.stage_id: stage for stage in stages}
    assigned = {name for _, _, members in definitions for name in members}
    operational = [stage for stage in stages if stage.stage_id not in assigned]
    groups = []
    for identity, title, members in definitions:
        primary = by_id.get(identity)
        present = [by_id[name] for name in members if name in by_id]
        supporting = [stage for stage in present if stage is not primary]
        if identity == "D":
            # Completed synthesis belongs in Overview, not Preparation. Keep
            # unfinished/failed operational steps available for recovery.
            supporting += [
                stage
                for stage in operational
                if stage.stage_id not in ("R", "R_FINAL") or stage.status != "D"
            ]
        errors = [
            stage
            for stage in present + (operational if identity == "D" else [])
            if stage.status == "E"
        ]
        supporting = [
            stage
            for stage in supporting
            if stage.status != "D"
            or stage.stage_id == "S"
            or getattr(stage, "ui_summary", None)
            or getattr(stage, "ui_findings", None)
            or getattr(stage, "ui_notes", None)
            or getattr(stage, "ui_result_detail", None)
        ]
        visible_evidence = [
            stage
            for stage in supporting
            if stage.stage_id != "R"
            or not (by_id.get("R_FINAL") and by_id["R_FINAL"].status == "D")
        ]
        badges = [review_stage_badge(stage) for stage in present]
        tone, label = "gray", _("Pending")
        if errors:
            tone, label = "red", _("Error")
        elif any(badge == ("red", _("Fail")) for badge in badges):
            tone, label = "red", _("Fail")
        elif any(stage.status == "R" for stage in present):
            tone, label = "blue", _("Running")
        elif any(
            review_stage_badge(stage) == ("orange", _("Partial"))
            and (stage is primary or _supporting_evidence_unavailable(stage))
            for stage in present
        ):
            tone, label = "orange", _("Partial")
        elif len(present) < len(members) or any(
            stage.status != "D" for stage in present
        ):
            tone, label = (
                ("orange", _("Partial"))
                if primary and primary.status == "D"
                else ("gray", _("Pending"))
            )
        elif any(badge == ("orange", _("Warning")) for badge in badges):
            tone, label = "orange", _("Warning")
        else:
            tone, label = "green", _("Pass")
        group = {
            "id": identity,
            "title": title,
            "stage_ids": [stage.stage_id for stage in present],
            "primary": primary,
            "show_primary": bool(
                primary
                and primary.status != "E"
                and not (
                    errors
                    and (
                        primary.status != "D"
                        or isinstance(primary.output_json, dict)
                        and primary.output_json.get("availability") == "unavailable"
                    )
                )
            ),
            "supporting": supporting,
            "errors": errors,
            "tone": tone,
            "status": label,
            "additional_findings": [
                finding
                for stage in visible_evidence
                for finding in getattr(stage, "ui_findings", [])
            ],
            "additional_notes": [
                note
                for stage in visible_evidence
                if not stage.stage_id.startswith(("P_", "EXTRACT_"))
                for note in getattr(stage, "ui_notes", [])
                if note["title"]
                in (
                    _("Recommended action"),
                    _("Uncertainties and limitations"),
                    _("Unresolved questions"),
                    _("Ambiguities and contradictions"),
                )
            ],
            "evidence_limited": any(
                _supporting_evidence_unavailable(stage) for stage in supporting
            ),
        }
        if identity == "H7":
            group["similarity"] = review_similarity(by_id.get("S"), primary)
            if (
                not errors
                and group["tone"] != "red"
                and primary
                and primary.status == "D"
                and group["similarity"]["incomplete"]
            ):
                group["tone"], group["status"] = "orange", _("Partial")
            elif (
                not errors and group["tone"] != "red" and group["similarity"]["manual"]
            ):
                group["tone"], group["status"] = "orange", _("Manual review")
        groups.append(group)
    return groups


def review_finding_section(finding, source, groups):
    """Route finding links using supplied evidence identities, never prose guesses.

    A synthesis can cite several subjects. Prefer an explicit specialist stage,
    then a specific artifact, before generic contract/solution evidence. If no
    section is established, omit the jump instead of inventing a Preparation link.
    """
    by_stage = {identity: group for group in groups for identity in group["stage_ids"]}
    if source.stage_id not in ("R", "R_FINAL"):
        return by_stage.get(source.stage_id)
    refs = finding.get("evidence_refs", [])
    refs = (
        [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []
    )
    explicit = [
        by_stage[ref[6:]]
        for ref in refs
        if ref.startswith("stage:") and ref[6:] in by_stage
    ]
    for group in explicit:
        if group["id"] != "D" and any(
            ref == "stage:" + group["id"]
            or ref.startswith("stage:" + group["id"] + "_")
            for ref in refs
        ):
            return group
    by_id = {group["id"]: group for group in groups}
    if "H7" in by_id and any(ref.startswith("candidate:") for ref in refs):
        return by_id["H7"]
    for identity, artifacts in (
        ("H4", ("artifact:testcase_validator",)),
        ("H5", ("artifact:custom_checker", "artifact:custom_checker_cpp")),
        ("H3", ("tests", "artifact:generator", "artifact:zipfile")),
    ):
        if (
            "H7" in by_id
            and identity in by_id
            and any(ref in artifacts for ref in refs)
        ):
            return by_id[identity]
    if explicit:
        return explicit[0]
    if (
        "H7" in by_id
        and "H2" in by_id
        and any(ref.startswith("solution:") for ref in refs)
    ):
        return by_id["H2"]
    return None


def review_similarity(retrieval, assessment):
    """A completed search is not a completed equivalence assessment."""
    result = {
        "title": _("Assessment incomplete"),
        "incomplete": True,
        "manual": False,
        "matches": [],
        "other": [],
    }
    if (
        not retrieval
        or not assessment
        or retrieval.status != "D"
        or assessment.status != "D"
    ):
        return result
    retrieved = retrieval.output_json if isinstance(retrieval.output_json, dict) else {}
    output = assessment.output_json if isinstance(assessment.output_json, dict) else {}
    candidates = retrieved.get("candidates")
    comparisons = output.get("comparisons")
    if (
        not isinstance(candidates, list)
        or not candidates
        or not isinstance(comparisons, list)
    ):
        return result
    refs = [row.get("ref") for row in candidates if isinstance(row, dict)]
    if len(refs) != len(candidates) or not all(
        isinstance(ref, str) and ref for ref in refs
    ):
        return result
    if len(set(refs)) != len(refs):
        return result
    labels = {
        "essentially_equivalent": _("Potential duplicate"),
        "meaningful_variation": _("Meaningful variation"),
        "shared_technique_only": _("Shared technique only"),
        "unrelated": _("No substantive match"),
        "uncertain": _("Uncertain relationship"),
    }
    compared = {}
    candidates_by_ref = {
        row.get("ref"): row
        for row in getattr(retrieval, "ui_candidates", [])
        if isinstance(row.get("ref"), str)
    }
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            return result
        ref, relationship = comparison.get("candidate_ref"), comparison.get(
            "relationship"
        )
        if (
            not isinstance(ref, str)
            or ref not in refs
            or ref in compared
            or not isinstance(relationship, str)
            or relationship not in labels
        ):
            return result
        differences = comparison.get("substantive_differences", [])
        compared[ref] = {
            "candidate": candidates_by_ref.get(ref, {}),
            "relationship": relationship,
            "label": labels[relationship],
            "tone": (
                "orange"
                if relationship in ("essentially_equivalent", "uncertain")
                else "blue"
            ),
            "reason": review_prose(comparison.get("core_comparison")),
            "differences": (
                [review_prose(item) for item in differences if isinstance(item, str)]
                if isinstance(differences, list)
                else []
            ),
            "significance": review_prose(comparison.get("algorithmic_significance")),
        }
    if set(compared) != set(refs):
        return result
    concerning = [
        item
        for item in compared.values()
        if item["relationship"] in ("essentially_equivalent", "uncertain")
    ]
    # Only possible duplicates and unresolved comparisons need prominence.
    # Substantive variations remain available as nonblocking reference material.
    result["matches"] = concerning
    result["other"] = [
        item for item in compared.values() if item not in result["matches"]
    ]
    result["manual"] = any(
        item["relationship"] == "essentially_equivalent" for item in concerning
    )
    result["incomplete"] = bool(
        output.get("_missing_evidence")
        or output.get("uncertainties")
        or output.get("availability") == "unavailable"
        or output.get("status") == "uncertain"
        or retrieved.get("limitations")
        or any(item["relationship"] == "uncertain" for item in concerning)
    )
    if result["manual"]:
        result["title"] = _("Potential duplicate — manual review")
    elif result["incomplete"]:
        result["title"] = _("Assessment incomplete")
    else:
        result["title"] = _("No concerning match found in assessed candidates")
    return result


def review_stage_notes(stage):
    """Only expose selected, readable results, never arbitrary output fields."""
    output = stage.output_json if isinstance(stage.output_json, dict) else {}
    notes = []

    def add(title, value):
        values = value if isinstance(value, list) else [value]
        items = [
            review_prose(item)
            for item in values
            if isinstance(item, str) and item.strip()
        ]
        if items:
            notes.append({"title": title, "items": list(dict.fromkeys(items))})

    if stage.stage_id == "D":
        if output.get("complete") is True:
            add(_("Preparation"), _("All required preparation checks passed."))
        add(_("Required fixes"), output.get("errors"))
    elif stage.stage_id == "E":
        add(_("Task"), output.get("task"))
        add(_("Input format"), output.get("input_structure"))
        add(_("Output requirements"), output.get("output_requirements"))
        add(_("Constraints"), output.get("constraints_and_aggregate_limits"))
        add(_("Subtasks and scoring"), output.get("subtasks_and_scoring"))
        add(_("Samples and explanations"), output.get("samples_and_explanations"))
        add(
            _("Ambiguities and contradictions"),
            output.get("ambiguities_or_contradictions"),
        )
    elif stage.stage_id == "FG":
        solver = output.get("F") if isinstance(output.get("F"), dict) else {}
        coordinator = output.get("G") if isinstance(output.get("G"), dict) else {}
        add(
            _("Algorithms and supported scopes"),
            solver.get("algorithms_and_supported_scopes"),
        )
        add(_("Correctness arguments"), solver.get("correctness_arguments"))
        add(_("Complexity"), solver.get("complexity"))
        add(_("Unresolved questions"), solver.get("unresolved_questions"))
        add(_("Difficulty assessment"), coordinator.get("difficulty_reasoning"))
        add(_("Core techniques"), coordinator.get("core_techniques"))
        add(_("Subtask analysis"), coordinator.get("subtask_analysis"))
        add(_("Wrong or slow approaches"), coordinator.get("wrong_or_slow_approaches"))
        add(_("Test coverage plan"), coordinator.get("coverage_blueprint"))
        add(_("Uncertainties and limitations"), coordinator.get("uncertainties"))
    add(_("Recommended action"), output.get("actions"))
    add(_("Uncertainties and limitations"), output.get("uncertainties"))
    add(_("Uncertainties and limitations"), output.get("limitations"))
    return notes


def review_stage_badge(stage):
    """Presentation only: completion is not proof of package readiness."""
    if stage.status == stage.ERROR:
        return "red", _("Error")
    if stage.status != stage.DONE:
        return (
            "blue" if stage.status == stage.RUNNING else "gray",
            stage.get_status_display(),
        )

    output = stage.output_json if isinstance(stage.output_json, dict) else {}
    findings = output.get("findings")
    severities = (
        {
            item.get("severity")
            for item in findings
            if isinstance(item, dict) and isinstance(item.get("severity"), str)
        }
        if isinstance(findings, list)
        else set()
    )
    if "blocking" in severities or output.get("complete") is False:
        return "red", _("Fail")
    if output.get("availability") == "unavailable":
        return "orange", _("Partial")
    if output.get("_missing_evidence"):
        return "orange", _("Partial")

    readiness = {
        "READY": ("green", _("Pass")),
        "READY_WITH_WARNINGS": ("orange", _("Warning")),
        "NEEDS_AUTHOR_FIX": ("red", _("Fail")),
        "MANUAL_REVIEW": ("orange", _("Partial")),
        "INCOMPLETE": ("orange", _("Partial")),
    }
    if stage.stage_id in ("R", "R_FINAL") and output.get("readiness") in readiness:
        return readiness[output["readiness"]]
    if (stage.stage_id == "EXTRACT_REPAIR" and output.get("needed") is False) or (
        stage.stage_id == "P_FOLLOWUP" and output.get("requested") is False
    ):
        return "gray", _("Skipped")
    if output.get("available") is False:
        return "orange", _("Partial")
    if (
        output.get("status") == "uncertain"
        or output.get("uncertainties")
        or output.get("limitations")
    ):
        return "orange", _("Partial")
    if output.get("status") == "finding" or "warning" in severities:
        return "orange", _("Warning")
    return "green", _("Pass")


def review_author_feedback(output):
    """Prefer reader feedback without changing stored technical analysis."""
    feedback = output.get("author_feedback")
    if isinstance(feedback, str) and feedback.strip():
        return feedback
    summary = output.get("summary")
    return summary if isinstance(summary, str) else ""


def review_list_indentation(value):
    """Adapt compact model list indentation to Python-Markdown's four spaces.

    Only explicit indented list items establish nesting; never infer hierarchy
    from headings or wording. Preserve fenced code and indented code blocks.
    This is a rendering adapter, not a rewrite of the saved AI response.
    """
    if not isinstance(value, str):
        return ""
    levels = []  # (source indentation, renderer indentation)
    fence = None
    loose = False
    lines = []
    for line in value.splitlines(keepends=True):
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            lines.append(line)
            if (
                fence_match
                and fence_match[1][0] == fence[0]
                and len(fence_match[1]) >= len(fence)
                and not line[fence_match.end() :].strip()
            ):
                fence = None
            continue
        if fence_match:
            fence = fence_match[1]
            lines.append(line)
            continue
        if not line.strip():
            lines.append(line)
            loose = bool(levels)
            continue
        match = re.match(r"^( *)(?:[-+*]|[0-9]+\.)[ \t]+\S", line)
        indent = len(line) - len(line.lstrip(" "))
        dedented = False
        while levels and indent < levels[-1][0]:
            levels.pop()
            dedented = True
        if match:
            # Python-Markdown also needs a block boundary when a loose nested
            # list returns to an ancestor; otherwise it can swallow the sibling.
            if dedented and loose and lines and lines[-1].strip():
                lines.append("\n")
            if not levels:
                # Four leading spaces outside a list mean an indented code block.
                if indent >= 4:
                    lines.append(line)
                    continue
                levels.append((indent, indent))
            elif indent > levels[-1][0]:
                delta = indent - levels[-1][0]
                if delta <= 4:
                    levels.append((indent, levels[-1][1] + 4))
                # A larger jump can be indented code containing a list marker.
        elif levels and indent <= levels[-1][0]:
            levels = []
            loose = False
        offset = levels[-1][1] - levels[-1][0] if levels else 0
        if not match and levels and indent - levels[-1][0] in (2, 3):
            offset += 4 - (indent - levels[-1][0])
        lines.append(" " * offset + line)
    return "".join(lines)


def review_prose(value):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if "\n" in text or "```" in text:
        return text
    markers = [
        marker
        for marker in re.finditer(r"(?<!\S)([1-9][0-9]?)\.\s+(?=[^\d\s])", text)
        if not text[: marker.start()].rstrip().endswith(("=", "<", ">"))
    ]
    if len(markers) < 2 or [int(m[1]) for m in markers] != list(
        range(1, len(markers) + 1)
    ):
        return text
    # Avoid interpreting numbered strings inside inline code as prose lists.
    if any(text[: m.start()].count("`") % 2 for m in markers):
        return text
    intro = text[: markers[0].start()].strip()
    items = [
        text[
            m.start() : (
                markers[index + 1].start() if index + 1 < len(markers) else len(text)
            )
        ].strip()
        for index, m in enumerate(markers)
    ]
    return "\n\n".join(([intro] if intro else []) + items)
