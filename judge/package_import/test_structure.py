"""
Materialize an imported problem's test structure into graded ProblemTestCase rows.

The AI import produces a `test_structure` block in summary.json describing how the
package's tests are organized (flat list, or subtasks/batches with per-subtask
points and scoring policy). This module turns that description into concrete
`ProblemTestCase` rows and regenerates `init.yml` via `ProblemDataCompiler`, so an
imported problem is immediately gradable.

Design (Approach C — trust but verify):
  * The uploaded `testdata.zip`'s member list is the ground truth. Every input/
    output the LLM references is resolved against the actual archive paths
    (exact match first, then a unique-basename match so a `Data/01.inp`
    reference still works whether the zip stores it flat or under a folder).
    References that don't resolve are dropped with a warning.
  * The path written to init.yml is the archive-relative path AS STORED in the
    zip (e.g. `Data/01.inp`), so the judge can find it. We do NOT flatten to a
    basename — that would make the judge look for a file that isn't there.
  * If the structure is unusable (no valid cases), fall back to auto-pairing
    files by path stem (e.g. `01.in`/`01.out`, or Polygon `01`/`01.a`).
  * All-zero / missing points are treated as "unspecified" and distributed
    evenly, so a package that ships `points: 0` everywhere still ends up scorable.
  * Subtask scoring maps to the `batch_scoring` field on the batch-start (S) row:
      - "each_test"       -> batch_scoring="sum", inside-case weight 1 each
                             (judge rescales to an even split of the subtask points)
      - "all_or_nothing"  -> batch_scoring="min", inside-case weight 1 each
                             (judge scores min(case_fraction) * subtask_points)
"""

import logging
import os
import zipfile

from judge.models import ProblemData, ProblemTestCase
from judge.utils.problem_data import ProblemDataCompiler

logger = logging.getLogger(__name__)

# Extensions we recognize when auto-pairing test files by stem.
INPUT_EXTS = (".in", ".inp", ".txt")
OUTPUT_EXTS = (".out", ".ans", ".a", ".sol", ".exp")

# ProblemTestCase.input_file / output_file are CharField(max_length=100); an
# archive path longer than this can't be stored, so such cases are dropped.
MAX_FILE_FIELD = 100


def zip_members(zip_path):
    """Return the list of archive-relative file paths inside a zip (no dir entries)."""
    with zipfile.ZipFile(zip_path) as zf:
        return [name for name in zf.namelist() if name and not name.endswith("/")]


def _index(members):
    """Build lookup helpers: the member set, and a basename->path map that only
    keeps basenames which are unambiguous (a duplicated basename maps to None)."""
    member_set = set(members)
    by_base = {}
    for path in members:
        base = os.path.basename(path)
        by_base[base] = None if base in by_base else path
    return member_set, by_base


def _resolve(ref, member_set, by_base):
    """Resolve an LLM-referenced file to an actual archive path, or None.
    Tries an exact match first, then a unique-basename match. Returns None (and
    logs) for a path too long to store or an unresolvable/ambiguous reference."""
    if not ref:
        return None
    path = ref if ref in member_set else by_base.get(os.path.basename(ref))
    if path is None:
        logger.warning("Import: could not resolve test file reference %r", ref)
        return None
    if len(path) > MAX_FILE_FIELD:
        logger.warning(
            "Import: dropping case; archive path exceeds %d chars: %s",
            MAX_FILE_FIELD,
            path,
        )
        return None
    return path


def _auto_pair(members):
    """Fallback: pair input/output files by shared stem (full path minus extension).

    Handles the common conventions:
      - `01.in` / `01.out`, `01.inp` / `01.ans`, `Data/01.inp` / `Data/01.out`
      - Polygon-style extensionless input `01` with answer `01.a`
    Returns a sorted list of (input_path, output_path) tuples.
    """
    member_set = set(members)
    by_stem_out = {}
    for name in members:
        stem, ext = os.path.splitext(name)
        if ext.lower() in OUTPUT_EXTS:
            by_stem_out.setdefault(stem, name)

    pairs = []
    for stem, out_name in by_stem_out.items():
        candidates = [stem + e for e in INPUT_EXTS] + [stem]
        input_name = next(
            (c for c in candidates if c in member_set and c != out_name), None
        )
        if input_name:
            pairs.append((input_name, out_name))

    return sorted(pairs, key=lambda p: p[0])


def _resolve_points(provided):
    """Given a list of per-item provided points, decide the final integer points.

    Trusts the provided values ONLY when every item carries a positive number —
    i.e. a complete, deliberate scheme (like Polygon subtask points 40/60).
    Any missing or zero value means the scheme is unreliable (a common LLM output
    is `points: 0` on every case, or a lone `1`), so we fall back to an even split
    of 1 point per item.

    Note: DMOJ case/subtask points are RELATIVE weights (a submission scores
    earned/total * Problem.points), so "1 each" is a clean, predictable even split
    and does not depend on Problem.points.
    """
    usable = provided and all(isinstance(p, (int, float)) and p > 0 for p in provided)
    if usable:
        return [int(p) for p in provided]
    return [1] * len(provided)


def _case_rows_flat(cases, member_set, by_base):
    """Build ProblemTestCase kwargs for flat cases, dropping any whose input
    can't be resolved. Returns (rows, dropped_count)."""
    valid = []
    dropped = 0
    for case in cases:
        in_path = _resolve(case.get("input"), member_set, by_base)
        out_ref = case.get("output")
        out_path = _resolve(out_ref, member_set, by_base)
        if not in_path:
            dropped += 1
            continue
        if out_ref and not out_path:
            # Output named but missing from the archive — drop to avoid a broken pair.
            dropped += 1
            continue
        valid.append((case, in_path, out_path or ""))

    points = _resolve_points([c.get("points") for c, _, _ in valid])

    rows = []
    for (case, in_path, out_path), pts in zip(valid, points):
        rows.append(
            {
                "type": "C",
                "input_file": in_path,
                "output_file": out_path,
                "points": pts,
                "is_pretest": bool(case.get("is_pretest", False)),
            }
        )
    return rows, dropped


def _case_rows_batched(subtasks, member_set, by_base):
    """Build rows for batched subtasks as S / C.../ E marker sequences.
    Returns (rows, dropped_count)."""
    prepared = []
    dropped = 0
    for st in subtasks:
        good = []
        for case in st.get("cases", []):
            in_path = _resolve(case.get("input"), member_set, by_base)
            out_ref = case.get("output")
            out_path = _resolve(out_ref, member_set, by_base)
            if not in_path:
                dropped += 1
                continue
            if out_ref and not out_path:
                dropped += 1
                continue
            good.append((in_path, out_path or ""))
        if good:
            prepared.append((st, good))

    if not prepared:
        return [], dropped

    subtask_points = _resolve_points([st.get("points") for st, _ in prepared])

    rows = []
    for (st, good), pts in zip(prepared, subtask_points):
        scoring = st.get("scoring", "each_test")
        batch_scoring = "min" if scoring == "all_or_nothing" else "sum"
        # Batch start marker carries the subtask's total points + scoring policy.
        rows.append(
            {
                "type": "S",
                "input_file": "",
                "output_file": "",
                "points": pts,
                "is_pretest": False,
                "batch_scoring": batch_scoring,
            }
        )
        # Each real case inside the batch is an equal relative weight (1).
        for in_path, out_path in good:
            rows.append(
                {
                    "type": "C",
                    "input_file": in_path,
                    "output_file": out_path,
                    "points": 1,
                    "is_pretest": False,
                }
            )
        rows.append(
            {
                "type": "E",
                "input_file": "",
                "output_file": "",
                "points": 0,
                "is_pretest": False,
            }
        )
    return rows, dropped


def _rows_from_auto_pair(members):
    """Build flat rows by auto-pairing files. Used as the last-resort fallback.
    Even split: 1 point per case."""
    rows = []
    for in_path, out_path in _auto_pair(members):
        rows.append(
            {
                "type": "C",
                "input_file": in_path,
                "output_file": out_path,
                "points": 1,
                "is_pretest": False,
            }
        )
    return rows


def materialize_test_structure(problem, structure, zip_path):
    """Create ProblemTestCase rows from an LLM `test_structure` and regenerate init.yml.

    Args:
        problem: the target Problem
        structure: the `test_structure` dict from summary.json (may be None/empty)
        zip_path: filesystem path to the uploaded testdata.zip

    Returns a short human-readable message describing what was created.
    Raises ProblemDataError (from ProblemDataCompiler) on unrecoverable config errors.
    """
    members = zip_members(zip_path)
    if not members:
        return "Test data uploaded, but the zip contains no files; no cases created."

    member_set, by_base = _index(members)
    structure = structure or {}
    kind = structure.get("kind")

    dropped = 0
    used_fallback = False

    if kind == "batched" and structure.get("subtasks"):
        rows, dropped = _case_rows_batched(structure["subtasks"], member_set, by_base)
    elif kind == "flat" and structure.get("cases"):
        rows, dropped = _case_rows_flat(structure["cases"], member_set, by_base)
    else:
        rows = []

    # Fallback: nothing usable from the declared structure -> auto-pair by stem.
    if not rows:
        rows = _rows_from_auto_pair(members)
        used_fallback = True

    if not rows:
        return (
            "Test data uploaded, but no input/output pairs could be identified. "
            "Configure test cases manually on the problem data page."
        )

    data, _created = ProblemData.objects.get_or_create(problem=problem)

    # Replace any existing cases so re-applying is idempotent.
    problem.cases.all().delete()
    for order, row in enumerate(rows):
        ProblemTestCase.objects.create(dataset=problem, order=order, **row)

    # `files` need only contain every referenced input/output; passing all archive
    # paths is safe (ProblemDataCompiler uses it purely for existence checks).
    ProblemDataCompiler.generate(
        problem,
        data,
        list(problem.cases.order_by("order")),
        sorted(member_set),
    )

    case_count = sum(1 for r in rows if r["type"] == "C")
    batch_count = sum(1 for r in rows if r["type"] == "S")
    logger.info(
        "Import materialized %s: %d cases, %d subtasks, dropped=%d, fallback=%s",
        problem.code,
        case_count,
        batch_count,
        dropped,
        used_fallback,
    )
    parts = []
    if batch_count:
        parts.append(f"{batch_count} subtask{'s' if batch_count != 1 else ''}")
    parts.append(f"{case_count} test case{'s' if case_count != 1 else ''}")
    msg = (
        "Test data uploaded; created " + ", ".join(parts) + " and regenerated init.yml."
    )
    if used_fallback:
        msg += " (Auto-paired files by name; review the scoring.)"
    if dropped:
        msg += f" Skipped {dropped} case(s) whose files were missing from the zip."
    return msg
