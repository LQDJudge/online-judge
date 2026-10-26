"""Frozen components for native function/interaction challenge jobs."""

import hashlib
import json
import os
import re

from judge.review.signature import signature_family
from judge.review.checker_options import valid_builtin_options
from judge.review.snapshots import MAX_SOURCE_BYTES, read_artifact
from judge.review.types import snapshot_problem_profile


def validate_profile_package(package):
    if not isinstance(package, dict) or package.get("execution") not in (
        "batch",
        "interactive",
        "communication",
    ):
        raise ValueError("Unsupported profile execution")
    if package.keys() - {
        "execution",
        "files",
        "signature",
        "component",
        "num_processes",
        "file_io",
        "checker",
        "binary",
    }:
        raise ValueError("Unknown profile package setting")
    files = package.get("files", {})
    if not isinstance(files, dict) or len(files) > 24:
        raise ValueError("Invalid profile file inventory")
    total = 0
    for name, item in files.items():
        if (
            not isinstance(name, str)
            or len(name) > 128
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name)
            or ".." in name
            or name.startswith("_review_")
            or not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
        ):
            raise ValueError("Invalid profile file")
        raw = item["source"].encode()
        total += len(raw)
        if (
            len(raw) > MAX_SOURCE_BYTES
            or total > 1024 * 1024
            or hashlib.sha256(raw).hexdigest() != item.get("sha256")
        ):
            raise ValueError("Invalid profile file identity/size")
    signature = package.get("signature")
    if signature is not None:
        if (
            not isinstance(signature, dict)
            or signature.keys() - {"family", "entry", "header"}
            or signature.get("family") not in ("c", "java", "python")
            or signature.get("entry") not in files
            or signature["family"] == "c"
            and signature.get("header") not in files
        ):
            raise ValueError("Invalid signature interface")
    if package["execution"] != "batch":
        component = package.get("component")
        if (
            not isinstance(component, dict)
            or component.keys() - {"files", "language", "type"}
            or not isinstance(component.get("files"), list)
            or not 0 < len(component["files"]) <= 8
            or any(name not in files for name in component["files"])
            or component.get("language")
            not in ("C", "C11", "C23", "CPP11", "CPP14", "CPP17", "CPP20", "CPP23")
            or component.get("type") not in ("default", "lqdoj", "testlib", "cms")
        ):
            raise ValueError("Invalid manager/interactor invocation")
    count = package.get("num_processes", 1)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 16:
        raise ValueError("Invalid profile process count")
    checker = package.get("checker")
    if checker is not None:
        if not isinstance(checker, dict):
            raise ValueError("Invalid profile checker")
        if checker.get("kind") == "builtin":
            if checker.keys() - {"kind", "name", "args"} or not valid_builtin_options(
                checker.get("name"), checker.get("args")
            ):
                raise ValueError("Invalid profile built-in checker")
        elif (
            checker.get("kind") != "source"
            or checker.keys() - {"kind", "filename", "language", "type", "args"}
            or checker.get("filename") not in files
            or checker.get("language")
            not in ("PY3", "CPP11", "CPP14", "CPP17", "CPP20", "CPP23")
            or checker.get("type") not in ("default", "testlib", "cms", "lqdoj")
            or not isinstance(checker.get("args"), dict)
        ):
            raise ValueError("Invalid profile source checker")
    if "binary" in package and not isinstance(package["binary"], bool):
        raise ValueError("Invalid binary data flag")
    file_io = package.get("file_io", {})
    if not isinstance(file_io, dict) or file_io.keys() - {"input", "output"}:
        raise ValueError("Invalid named I/O")
    for name in file_io.values():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", name)
            or ".." in name
            or name.startswith("_review_")
        ):
            raise ValueError("Unsafe named I/O")
    return package


def build_profile_package(snapshot, runtime, checker_config=None):
    profile = snapshot_problem_profile(snapshot)
    data = snapshot.get("data_config", {})
    package = {"execution": profile.execution, "files": {}}
    package["binary"] = profile.binary
    available = set()
    for observation in [snapshot.get("validation") or {}] + [
        item.get("evidence") or {} for item in snapshot.get("solutions", [])
    ]:
        binding = (
            observation.get("review_evidence")
            or observation.get("evidence_snapshot")
            or {}
        )
        available.update(binding.get("environment", {}).get("runtimes", {}))
    cpp_runtime = next(
        (
            key
            for key in ("CPP23", "CPP20", "CPP17", "CPP14", "CPP11")
            if key in available
        ),
        "CPP20",
    )

    def add(key):
        artifact = snapshot.get("artifacts", {}).get(key)
        if not artifact:
            raise ValueError("Required profile component missing")
        raw = read_artifact(artifact, MAX_SOURCE_BYTES)
        item = {"source": raw.decode("utf8"), "sha256": artifact["sha256"]}
        name = artifact["filename"]
        if name in package["files"] and package["files"][name] != item:
            raise ValueError("Conflicting profile component filenames")
        package["files"][name] = item
        return name

    for key in snapshot.get("artifacts", {}):
        if key.startswith("aux:"):
            add(key)
    if profile.signature:
        family = signature_family(runtime)
        if not family:
            raise ValueError("Unsupported signature runtime")
        signature = {"family": family, "entry": add("signature:%s:handler" % family)}
        if family == "c":
            signature["header"] = add("signature:c:header")
        package["signature"] = signature
    if profile.execution in ("interactive", "communication"):
        key = (
            "interactive_judge"
            if profile.execution == "interactive"
            else "communication_manager"
        )
        name = add(key)
        # The site compiler uses the latest installed C++ runtime. Reuse its
        # frozen effective setting when provided, never a model-selected command.
        effective = snapshot.get("effective_profile_config", {})
        config = (
            effective.get("interactive", {})
            if profile.execution == "interactive"
            else effective.get("communication", {}).get("manager", {})
        )
        choices = (
            ("C11", "C")
            if os.path.splitext(name)[1] == ".c"
            else ("CPP23", "CPP20", "CPP17", "CPP14", "CPP11")
        )
        runtime_key = config.get("lang") or next(
            (key for key in choices if key in available), choices[0]
        )
        package["component"] = {
            "files": [name],
            "language": runtime_key,
            "type": (
                config.get("type")
                or ("lqdoj" if data.get("checker") == "interact" else "testlib")
                if profile.execution == "interactive"
                else effective.get("communication", {}).get("type")
                or ("cms" if data.get("checker") == "testlibcms" else "default")
            ),
        }
    if profile.execution == "communication":
        package["num_processes"] = data.get("communication_num_processes")
    package["file_io"] = {
        key: data[field]
        for key, field in (("input", "fileio_input"), ("output", "fileio_output"))
        if data.get(field)
    }
    selected = checker_config or data
    checker_name = selected.get("checker") or "standard"
    raw_args = selected.get("checker_args") or "{}"
    checker_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    if valid_builtin_options(checker_name, checker_args):
        package["checker"] = {
            "kind": "builtin",
            "name": checker_name,
            "args": checker_args,
        }
    elif checker_name in ("custom", "customcpp", "testlib", "testlibcms"):
        key = "custom_checker" if checker_name == "custom" else "custom_checker_cpp"
        if snapshot.get("artifacts", {}).get(key):
            filename = add(key)
            effective = snapshot.get("effective_profile_config", {}).get("checker")
            if (
                checker_name != "custom"
                and isinstance(effective, dict)
                and effective.get("name") == "bridged"
            ):
                cpp_runtime = (effective.get("args") or {}).get("lang") or cpp_runtime
            package["checker"] = {
                "kind": "source",
                "filename": filename,
                "language": "PY3" if checker_name == "custom" else cpp_runtime,
                "type": (
                    "testlib"
                    if checker_name == "testlib"
                    else "cms" if checker_name == "testlibcms" else "lqdoj"
                ),
                "args": checker_args,
            }
        elif not (
            profile.execution == "communication" and checker_name == "testlibcms"
        ):
            raise ValueError("Frozen final checker source is unavailable")
    elif checker_name not in ("interact", "interacttl"):
        raise ValueError("Frozen final checker configuration cannot be reproduced")
    return validate_profile_package(package)
