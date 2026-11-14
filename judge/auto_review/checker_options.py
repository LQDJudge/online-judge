"""Bounded effective checker configurations for source-based assessment."""

import json


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
