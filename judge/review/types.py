"""Composable judging capabilities; identification never grants review support."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import PurePosixPath

from django.utils.translation import gettext_lazy as _

PROFILE_VERSION = 1
PROFILE_LABELS = {
    "standard": _("Standard"),
    "signature": _("Function / IOI signature"),
    "interactive": _("Interactive"),
    "communication": _("Communication"),
    "output_only": _("Output-only"),
    "metric": _("Metric-scored"),
    "custom": _("Custom grader"),
}
BUILTIN_CHECKERS = frozenset(
    (
        "standard",
        "easy",
        "floats",
        "floatsabs",
        "floatsrel",
        "rstripped",
        "sorted",
        "identical",
        "linecount",
        "linematches",
        "unordered",
        "bridged",
        "csv_accuracy",
        "csv_rmse",
        "csv_mae",
        "csv_f1",
        "csv_auc",
        "csv_logloss",
    )
)


def _effective_checker_names(config):
    """Include case and subtask overrides; bound even direct in-memory callers."""
    stack = [(config, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 100000 or depth > 64:
            raise ValueError("Invalid checker tree")
        if isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
        elif isinstance(node, Mapping):
            if "checker" in node:
                checker = node["checker"]
                if isinstance(checker, Mapping):
                    checker = checker.get("name")
                if not isinstance(checker, str):
                    raise ValueError("Invalid checker")
                yield checker
            for key in ("test_cases", "batched"):
                if key in node:
                    stack.append((node[key], depth + 1))


@dataclass(frozen=True)
class ProblemJudgingProfile:
    execution: str
    submission: str
    signature: bool
    metric: bool
    binary: bool
    issues: tuple
    effective_config_checked: bool

    @property
    def tags(self):
        tags = []
        if self.execution in ("interactive", "communication", "custom"):
            tags.append(self.execution)
        if self.submission == "output":
            tags.append("output_only")
        if self.signature:
            tags.append("signature")
        if self.metric:
            tags.append("metric")
        return tuple(tags or ("standard",))

    @property
    def review_supported(self):
        # Preparation and evidence adequacy remain independent hard gates.
        return not self.issues and self.execution in (
            "batch",
            "interactive",
            "communication",
        )

    def as_dict(self):
        value = asdict(self)
        value.update(version=PROFILE_VERSION, tags=list(self.tags))
        value["issues"] = list(self.issues)
        return value


def resolve_problem_profile(data_config=None, *, effective_config=None, artifacts=None):
    """Identify authoring settings and optional actual init.yml without AI.

    Native selection uses key presence, including a false-valued output_only
    key. Preserve that precedence for diagnosis, but flag shadowed modes.
    """
    config = {} if data_config is None else data_config
    if not isinstance(config, Mapping):
        config = {}
        issues = ["invalid_authoring_config"]
    else:
        issues = []
    checker = config.get("checker") or ""
    checker = checker if isinstance(checker, str) else ""
    count = config.get("communication_num_processes")
    valid_count = (
        isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 16
    )
    manager = bool(config.get("communication_manager")) or bool(
        (artifacts or {}).get("communication_manager")
    )
    declared = {
        "interactive": checker in ("interact", "interacttl"),
        "output_only": bool(config.get("output_only")),
        # The compiler emits communication independently of is_communication.
        "communication": manager and valid_count,
    }
    if manager and not valid_count:
        issues.append("invalid_communication_process_count")
    if config.get("is_communication") and not declared["communication"]:
        issues.append("communication_manager_required")
    signature = bool(config.get("use_ioi_signature"))
    metric = checker.startswith("csv_")
    if metric and checker not in BUILTIN_CHECKERS:
        issues.append("unregistered_checker")
    binary = bool(config.get("binary_data"))
    checked = effective_config is not None
    active = declared.copy()
    active["custom"] = False
    if checked:
        if not isinstance(effective_config, Mapping):
            issues.append("invalid_effective_config")
            effective_config = {}
        active = {
            mode: key in effective_config
            for mode, key in (
                ("custom", "custom_judge"),
                ("interactive", "interactive"),
                ("output_only", "output_only"),
                ("communication", "communication"),
            )
        }
        for mode, enabled in declared.items():
            if enabled != active[mode]:
                issues.append("configuration_mismatch_" + mode)
        communication = effective_config.get("communication")
        effective_signature = "signature_grader" in effective_config or (
            isinstance(communication, Mapping) and "signature" in communication
        )
        if signature != effective_signature:
            issues.append("configuration_mismatch_signature")
        signature = signature or effective_signature
        if active["communication"]:
            processes = (
                communication.get("num_processes", 1)
                if isinstance(communication, Mapping)
                else None
            )
            if (
                not isinstance(processes, int)
                or isinstance(processes, bool)
                or not 1 <= processes <= 16
            ):
                issues.append("invalid_effective_process_count")
            elif declared["communication"] and processes != count:
                issues.append("configuration_mismatch_process_count")
        legacy_checker = str(
            PurePosixPath(
                str(
                    config.get("custom_checker")
                    or ((artifacts or {}).get("custom_checker") or {}).get("filename")
                    or ""
                )
            ).name
        )
        try:
            for effective_checker in _effective_checker_names(effective_config):
                metric = metric or effective_checker.startswith("csv_")
                if effective_checker not in BUILTIN_CHECKERS and not (
                    checker == "custom" and effective_checker == legacy_checker
                ):
                    issues.append("unregistered_checker")
        except ValueError:
            issues.append("invalid_effective_checker")
        binary = binary or bool(effective_config.get("binary_data"))
    if sum(active.values()) > 1:
        issues.append("conflicting_execution_modes")
    execution = next(
        (
            mode
            for mode in ("custom", "interactive", "output_only", "communication")
            if active[mode]
        ),
        "batch",
    )
    if execution == "output_only":
        execution = "batch"
    output = active["output_only"] or declared["output_only"]
    if output and signature:
        issues.append("output_signature_conflict")
    if active["custom"]:
        issues.append("unregistered_custom_grader")
    return ProblemJudgingProfile(
        execution=execution,
        submission="output" if output else "source",
        signature=signature,
        metric=metric,
        binary=binary,
        issues=tuple(dict.fromkeys(issues)),
        effective_config_checked=checked,
    )


def is_supported_problem(data_config):
    """Recognized authoring profile; preparation/execution gates still apply."""
    return resolve_problem_profile(data_config).review_supported


def is_supported_batch_problem(data_config):
    """Legacy import alias; new callers should use is_supported_problem."""
    return is_supported_problem(data_config)


def snapshot_problem_profile(snapshot):
    """Use the persisted effective resolution; legacy runs retain saved settings."""
    profile = snapshot.get("judging_profile")
    if profile is not None:
        if (
            isinstance(profile, dict)
            and profile.get("version") == PROFILE_VERSION
            and profile.get("execution")
            in ("batch", "interactive", "communication", "custom")
            and profile.get("submission") in ("source", "output")
            and all(
                type(profile.get(key)) is bool
                for key in ("signature", "metric", "binary", "effective_config_checked")
            )
            and isinstance(profile.get("issues"), (list, tuple))
            and all(isinstance(issue, str) for issue in profile["issues"])
        ):
            return ProblemJudgingProfile(
                execution=profile["execution"],
                submission=profile["submission"],
                signature=profile["signature"],
                metric=profile["metric"],
                binary=profile["binary"],
                issues=tuple(profile["issues"]),
                effective_config_checked=profile["effective_config_checked"],
            )
        return replace(
            resolve_problem_profile(
                snapshot.get("data_config"), artifacts=snapshot.get("artifacts")
            ),
            issues=("invalid_saved_profile",),
        )
    return resolve_problem_profile(
        snapshot.get("data_config"), artifacts=snapshot.get("artifacts")
    )


def problem_profile_labels(snapshot):
    """Describe the selected run, never today's edited package."""
    return [PROFILE_LABELS[tag] for tag in snapshot_problem_profile(snapshot).tags]
