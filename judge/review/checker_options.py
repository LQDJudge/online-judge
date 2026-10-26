"""Bounded options for installed, trusted native checker probes."""

import hashlib
import json
import math


def checker_configurations(snapshot):
    """Unique effective case checker settings, including per-case overrides."""
    root = snapshot.get("data_config", {})
    groups = {}
    position = 0
    for row in snapshot.get("tests", []):
        if row.get("type") != "C":
            continue
        position += 1
        override = bool(row.get("checker"))
        name = row.get("checker") if override else root.get("checker") or "standard"
        args = row.get("checker_args") if override else root.get("checker_args")
        args = args or "{}"
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
            identity = json.dumps(parsed, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            identity = str(args)
        group = groups.setdefault(
            (name, identity),
            {"checker": name, "checker_args": args, "positions": [], "case_count": 0},
        )
        group["case_count"] += 1
        if len(group["positions"]) < 80:
            group["positions"].append(position)
    if not groups:
        groups[(root.get("checker") or "standard", "")] = {
            "checker": root.get("checker") or "standard",
            "checker_args": root.get("checker_args") or "{}",
            "positions": [],
            "case_count": 0,
        }
    return [
        {
            "scope": "checker-%s" % index,
            **group,
            "omitted_positions": max(0, group["case_count"] - len(group["positions"])),
        }
        for index, group in enumerate(groups.values(), 1)
    ]


def metric_public_hints(configurations):
    """Known small experiment IDs, using the native scorer's exact mask rule.

    This supplies no reference labels and makes no claim about dataset coverage.
    """
    hints = []
    for config in configurations:
        if not config["checker"].startswith("csv_"):
            continue
        raw = config.get("checker_args") or "{}"
        try:
            options = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if not valid_builtin_options(config["checker"], options):
            continue
        fraction = options.get("pretest_fraction", 1)
        ids = ["a", "b", "c", "d"] + [str(index) for index in range(32)]
        hints.append(
            {
                "scope": config["scope"],
                "fraction": fraction,
                "experiment_ids": {
                    value: int.from_bytes(
                        hashlib.md5(value.encode()).digest()[:4], "big"
                    )
                    % 1000
                    < int(fraction * 1000)
                    for value in ids
                },
                "meaning": "True means included in public/pretest scoring. Use these IDs in small independently calculated fixtures; these are not official dataset rows.",
            }
        )
    return hints


BUILTIN_PROBE_CHECKERS = frozenset(
    (
        "standard",
        "easy",
        "identical",
        "rstripped",
        "floats",
        "floatsabs",
        "floatsrel",
        "sorted",
        "unordered",
        "linecount",
        "linematches",
        "csv_accuracy",
        "csv_rmse",
        "csv_mae",
        "csv_f1",
        "csv_auc",
        "csv_logloss",
    )
)
CHECKER_OPTION_KEYS = frozenset(
    (
        "precision",
        "error_mode",
        "has_header",
        "id_column",
        "label_column",
        "pretest_fraction",
        "pretests_only",
        "baseline",
        "split_on",
        "separator",
    )
)


def valid_builtin_options(name, options):
    if name not in BUILTIN_PROBE_CHECKERS or not isinstance(options, dict):
        return False
    if options.keys() - CHECKER_OPTION_KEYS:
        return False
    for key, value in options.items():
        if key in ("has_header", "pretests_only"):
            if not isinstance(value, bool):
                return False
        elif key == "precision":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 64
            ):
                return False
        elif key in ("pretest_fraction", "baseline"):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not -1e308 <= value <= 1e308
                or not math.isfinite(value)
            ):
                return False
            if key == "pretest_fraction" and not 0 < value <= 1:
                return False
        elif key == "error_mode":
            if value not in ("absolute", "relative", "default"):
                return False
        elif (
            not isinstance(value, (str, int))
            or isinstance(value, bool)
            or len(str(value)) > 128
        ):
            return False
    return True
