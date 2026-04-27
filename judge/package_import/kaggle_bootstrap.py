"""
Bootstrap a Kaggle-style output-only problem from a single labeled CSV.

Author uploads one CSV (id, features..., label). We split rows into train/test
by deterministic hash of the id (or row index), then produce:
- train.csv         — train portion, full rows  → attachment ("Training data")
- test_input.csv    — test portion, features only (label dropped) → attachment ("Test input")
- sample_submission.csv — test ids + a placeholder label → attachment ("Sample submission")
- testdata.zip      — contains 1.in (dummy) + 1.out (the test labels) → applied as ProblemData.zipfile
- summary.json      — drives the existing import-page rendering

The Apply buttons in the existing flow then commit:
- output_only=True
- checker=csv_<metric>, checker_args={id_column, label_column, has_header}
- attachments
- testdata.zip
"""

import csv
import hashlib
import io
import json
import os
import tempfile
import zipfile
from typing import List, Tuple

VALID_METRICS = {
    "csv_accuracy",
    "csv_rmse",
    "csv_mae",
    "csv_f1",
    "csv_auc",
    "csv_logloss",
}


def _row_in_train(row_id: str, train_ratio: float) -> bool:
    """Hash-based deterministic split. train_ratio in (0, 1)."""
    if train_ratio >= 1.0:
        return True
    if train_ratio <= 0.0:
        return False
    h = hashlib.md5(row_id.encode("utf-8")).digest()
    bucket = int.from_bytes(h[:4], "big") % 1000
    return bucket < int(train_ratio * 1000)


def _extract_csv_blob(blob: bytes, hint_name: str = "") -> bytes:
    """Accept either a raw CSV or a zip containing a single CSV. Returns CSV bytes."""
    # Quick zip sniff: zip files start with "PK".
    if blob[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                csvs = [
                    n
                    for n in zf.namelist()
                    if n.lower().endswith(".csv") and not n.startswith("__MACOSX")
                ]
                if not csvs:
                    raise ValueError("Zip contains no .csv files.")
                if len(csvs) > 1:
                    raise ValueError(
                        f"Zip contains multiple CSVs ({len(csvs)}). Please upload a zip with exactly one CSV."
                    )
                with zf.open(csvs[0]) as f:
                    return f.read()
        except zipfile.BadZipFile:
            raise ValueError("Invalid zip file.")
    return blob


def _read_csv_rows(blob: bytes, has_header: bool):
    """Returns (header, rows). header is None when has_header=False.
    `blob` may be raw CSV bytes or a single-CSV zip."""
    blob = _extract_csv_blob(blob)
    text = blob.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("Empty CSV.")
    if has_header:
        return rows[0], rows[1:]
    return None, rows


def _resolve_columns(header, has_header, id_column, label_column):
    """Returns (id_idx_or_None, label_idx). label_idx is required."""
    if has_header:
        if not header:
            raise ValueError("CSV has no header row.")
        if id_column:
            if id_column not in header:
                raise ValueError(
                    f"id_column '{id_column}' not found in header {header}."
                )
            id_idx = header.index(id_column)
        else:
            id_idx = None
        if not label_column:
            raise ValueError("label_column is required.")
        if label_column not in header:
            raise ValueError(
                f"label_column '{label_column}' not found in header {header}."
            )
        label_idx = header.index(label_column)
    else:
        try:
            id_idx = int(id_column) if id_column else None
        except ValueError:
            raise ValueError(
                "id_column must be a non-negative integer when has_header is off."
            )
        try:
            label_idx = int(label_column) if label_column else 0
        except ValueError:
            raise ValueError(
                "label_column must be a non-negative integer when has_header is off."
            )
    return id_idx, label_idx


def _write_csv(path: str, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if header is not None:
            w.writerow(header)
        w.writerows(rows)


def bootstrap(
    csv_blob: bytes,
    metric: str,
    train_ratio: float,
    id_column: str,
    label_column: str,
    has_header: bool,
    notes: str = "",
) -> Tuple[str, List[dict], dict]:
    """
    Split the input CSV and write artifacts to a fresh save_dir.

    Returns (save_dir, saved_files, summary) shaped like get_import_result()
    so the existing import-page rendering can consume it.

    Raises ValueError on malformed input.
    """
    if metric not in VALID_METRICS:
        raise ValueError(
            f"Unknown metric '{metric}'. Choose one of: {sorted(VALID_METRICS)}"
        )
    if not (0.0 < train_ratio < 1.0):
        raise ValueError("train_ratio must be strictly between 0 and 1 (e.g. 0.8).")

    header, rows = _read_csv_rows(csv_blob, has_header)
    id_idx, label_idx = _resolve_columns(header, has_header, id_column, label_column)

    train_rows, test_rows = [], []
    for row_no, row in enumerate(rows):
        if len(row) <= label_idx:
            continue
        rid = row[id_idx] if id_idx is not None else str(row_no)
        if _row_in_train(rid, train_ratio):
            train_rows.append(row)
        else:
            test_rows.append(row)

    if not train_rows or not test_rows:
        raise ValueError(
            f"Split produced empty side: {len(train_rows)} train / {len(test_rows)} test. "
            "Try a different ratio or check the input has enough rows."
        )

    # test_input.csv: drop the label column
    def drop_label(row):
        return [c for i, c in enumerate(row) if i != label_idx]

    test_input_header = drop_label(header) if header is not None else None
    test_input_rows = [drop_label(r) for r in test_rows]

    # test_answer.csv: keep id + label columns only (this is what the checker
    # compares against). When id is None, fall back to writing index + label.
    if header is not None:
        if id_idx is not None:
            answer_header = [header[id_idx], header[label_idx]]
            answer_rows = [[r[id_idx], r[label_idx]] for r in test_rows]
        else:
            answer_header = ["row", header[label_idx]]
            answer_rows = [[str(i), r[label_idx]] for i, r in enumerate(test_rows)]
    else:
        if id_idx is not None:
            answer_header = None
            answer_rows = [[r[id_idx], r[label_idx]] for r in test_rows]
        else:
            answer_header = None
            answer_rows = [[r[label_idx]] for r in test_rows]

    # sample_submission.csv: same SHAPE as the answer key but only a small
    # head sample (20 rows) — solvers derive the full submission format from
    # this. Keeping it small avoids bloating storage for big test sets.
    SAMPLE_LIMIT = 20
    placeholder = train_rows[0][label_idx] if train_rows else "0"
    sample_test = test_rows[:SAMPLE_LIMIT]
    if header is not None:
        if id_idx is not None:
            sample_header = [header[id_idx], header[label_idx]]
            sample_rows = [[r[id_idx], placeholder] for r in sample_test]
        else:
            sample_header = ["row", header[label_idx]]
            sample_rows = [[str(i), placeholder] for i, _ in enumerate(sample_test)]
    else:
        sample_header = None
        if id_idx is not None:
            sample_rows = [[r[id_idx], placeholder] for r in sample_test]
        else:
            sample_rows = [[placeholder] for _ in sample_test]

    save_dir = tempfile.mkdtemp(prefix="kaggle_bootstrap_")

    # Public attachments
    train_path = os.path.join(save_dir, "train.csv")
    _write_csv(train_path, header, train_rows)
    test_input_path = os.path.join(save_dir, "test_input.csv")
    _write_csv(test_input_path, test_input_header, test_input_rows)
    sample_path = os.path.join(save_dir, "sample_submission.csv")
    _write_csv(sample_path, sample_header, sample_rows)

    # Hidden test answer goes inside testdata.zip
    test_answer_path = os.path.join(save_dir, "test_answer.csv")
    _write_csv(test_answer_path, answer_header, answer_rows)
    test_input_in = os.path.join(save_dir, "1.in")
    with open(test_input_in, "w", encoding="utf-8") as f:
        f.write("unused\n")

    testdata_path = os.path.join(save_dir, "testdata.zip")
    with zipfile.ZipFile(testdata_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(test_input_in, "1.in")
        zf.write(test_answer_path, "test_answer.csv")

    # Summary drives the existing renderResults() in package_import.js
    summary = {
        "format": "kaggle_bootstrap",
        "problem_name": None,
        "test_count": len(test_rows),
        "sample_count": 0,
        "output_only": True,
        "csv_checker": {
            "metric": metric,
            "id_column": id_column or "",
            "label_column": label_column or "",
            "has_header": has_header,
            "baseline": None,
        },
        "attachments": [
            {
                "name": "train.csv",
                "description": f"Training data ({len(train_rows)} rows)",
            },
            {
                "name": "test_input.csv",
                "description": f"Test input — predict labels for these {len(test_rows)} rows",
            },
            {
                "name": "sample_submission.csv",
                "description": (
                    f"Sample submission format ({len(sample_rows)} rows"
                    + (
                        f"; extend to all {len(test_rows)} test ids)"
                        if len(sample_rows) < len(test_rows)
                        else ")"
                    )
                ),
            },
        ],
        "notes": notes,
    }
    summary_path = os.path.join(save_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    saved_files = [
        {
            "name": "summary.json",
            "size": os.path.getsize(summary_path),
            "path": summary_path,
            "content_type": "application/json",
        },
        {
            "name": "train.csv",
            "size": os.path.getsize(train_path),
            "path": train_path,
            "content_type": "text/csv",
        },
        {
            "name": "test_input.csv",
            "size": os.path.getsize(test_input_path),
            "path": test_input_path,
            "content_type": "text/csv",
        },
        {
            "name": "sample_submission.csv",
            "size": os.path.getsize(sample_path),
            "path": sample_path,
            "content_type": "text/csv",
        },
        {
            "name": "testdata.zip",
            "size": os.path.getsize(testdata_path),
            "path": testdata_path,
            "content_type": "application/zip",
        },
    ]
    return save_dir, saved_files, summary
