"""Bounded, non-executable reading of effective judge configuration."""

import json

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

MAX_PROFILE_CONFIG_BYTES = 1024 * 1024


def mismatched_judging_components(snapshot):
    """Compare compiled file references with the source bytes we can review.

    Old frozen snapshots without this projection retain their original policy.
    No file is opened and no configured module is imported here.
    """
    native = snapshot.get("effective_profile_config", {})
    if not isinstance(native, dict):
        return ["checker"]
    artifacts = snapshot.get("artifacts", {})
    data = snapshot.get("data_config", {})
    mismatches = set()

    def matches(files, artifact):
        return (
            isinstance(artifact, dict)
            and bool(artifact.get("filename"))
            and (files if isinstance(files, list) else [files])
            == [artifact["filename"]]
        )

    if "checker" in native:
        checker = native["checker"]
        name = checker.get("name") if isinstance(checker, dict) else checker
        args = checker.get("args", {}) if isinstance(checker, dict) else {}
        declared = data.get("checker") or "standard"
        expected_name = declared
        try:
            expected_args = json.loads(data.get("checker_args") or "{}")
        except (TypeError, ValueError):
            expected_args = None
        if declared in ("customcpp", "testlib", "testlibcms") and artifacts.get(
            "custom_checker_cpp"
        ):
            expected_name = "bridged"
            if not isinstance(args, dict) or not matches(
                args.get("files"), artifacts["custom_checker_cpp"]
            ):
                mismatches.add("checker")
            elif isinstance(expected_args, dict):
                expected_args = dict(expected_args)
                expected_args.update(
                    files=artifacts["custom_checker_cpp"]["filename"],
                    type={
                        "customcpp": "lqdoj",
                        "testlib": "testlib",
                        "testlibcms": "cms",
                    }[declared],
                )
                expected_args.pop("lang", None)
                if declared == "testlib":
                    expected_args.pop("treat_checker_points_as_percentage", None)
                    expected_args["treat_checker_points_as_fraction"] = True
                args = {key: value for key, value in args.items() if key != "lang"}
        elif declared == "custom":
            expected_name = (artifacts.get("custom_checker") or {}).get("filename")
        elif (
            declared in ("interact", "interacttl")
            or declared == "testlibcms"
            and "communication" in native
            and not artifacts.get("custom_checker_cpp")
        ):
            expected_name, expected_args = "standard", {}
        elif declared == "bridged":
            mismatches.add("checker")
        if name != expected_name or args != expected_args:
            mismatches.add("checker")
    if "interactive" in native:
        component = native["interactive"]
        if (
            not isinstance(component, dict)
            or not matches(component.get("files"), artifacts.get("interactive_judge"))
            or component.get("args_format_string")
        ):
            mismatches.add("interactor")
    if "communication" in native:
        communication = native["communication"]
        manager = (
            communication.get("manager", {}) if isinstance(communication, dict) else {}
        )
        if not isinstance(manager, dict) or not matches(
            manager.get("files"), artifacts.get("communication_manager")
        ):
            mismatches.add("manager")
    communication = native.get("communication")
    signature = native.get("signature_grader") or (
        communication.get("signature") if isinstance(communication, dict) else None
    )
    if isinstance(signature, dict):
        for row in snapshot.get("signature_graders", []):
            if not row.get("active"):
                continue
            family = row.get("family") or row.get("language")
            config = signature if family == "c" else signature.get(family, {})
            if (
                not isinstance(config, dict)
                or not matches(
                    config.get("entry"), artifacts.get("signature:%s:handler" % family)
                )
                or family == "c"
                and not matches(
                    config.get("header"), artifacts.get("signature:c:header")
                )
            ):
                mismatches.add("signature")
    return sorted(mismatches)


class ProfileConfigError(ValueError):
    pass


class _ProfileLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as error:
                raise ProfileConfigError("Invalid configuration key") from error
            if duplicate:
                raise ProfileConfigError("Duplicate configuration key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_profile_config(raw):
    """Reject aliases, duplicate keys and excessive nesting before construction.

    This reads configuration only. It never imports a configured grader or runs
    YAML constructors outside SafeLoader's built-in data types.
    """
    if len(raw) > MAX_PROFILE_CONFIG_BYTES:
        raise ProfileConfigError("Configuration exceeds review parsing limit")
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(raw)):
            if count >= 100000 or isinstance(event, AliasEvent):
                raise ProfileConfigError(
                    "Configuration exceeds review structure limits"
                )
            if isinstance(event, CollectionStartEvent):
                depth += 1
                if depth > 64:
                    raise ProfileConfigError(
                        "Configuration nesting exceeds review limit"
                    )
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
        value = yaml.load(raw, Loader=_ProfileLoader)
    except (
        yaml.YAMLError,
        UnicodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ProfileConfigError("Unreadable judge configuration") from error
    if not isinstance(value, dict):
        raise ProfileConfigError("Judge configuration must be a mapping")
    return value
