"""Contestant interface families supported by the native signature grader.

Keep this mapping aligned with dmoj.graders.base.SIGNATURE_GRADER_LANGUAGES
in judge-server. Unknown runtimes must not inherit a different family's interface.
"""

from copy import deepcopy

SIGNATURE_RUNTIMES = {
    "c": frozenset(
        (
            "C",
            "C11",
            "C23",
            "CPP03",
            "CPP11",
            "CPP14",
            "CPP17",
            "CPP20",
            "CPP23",
            "CPPTHEMIS",
            "CLANG",
            "CLPP14",
            "CLPP17",
            "CLPP20",
            "CLPP23",
        )
    ),
    "java": frozenset(
        ("JAVA", "JAVA8", "JAVA9", "JAVA10", "JAVA11", "JAVA15", "JAVA17")
    ),
    "python": frozenset(("PY2", "PY3", "PYPY", "PYPY3")),
}


def signature_family(runtime):
    return next(
        (family for family, keys in SIGNATURE_RUNTIMES.items() if runtime in keys), None
    )


def enabled_signature_families(runtime_keys):
    return {family for key in runtime_keys if (family := signature_family(key))}


def relevant_signature_config(config, runtime_keys):
    """Unused interface families do not change effective judging semantics.

    Raw init.yml is still frozen/attested. This projection is only the review
    and execution identity for a signature-enabled package.
    """
    result = deepcopy(config)
    families = enabled_signature_families(runtime_keys)
    groups = [result.get("signature_grader")]
    communication = result.get("communication")
    if isinstance(communication, dict):
        groups.append(communication.get("signature"))
    for group in groups:
        if not isinstance(group, dict):
            continue
        if "c" not in families:
            group.pop("entry", None)
            group.pop("header", None)
        for family in ("java", "python"):
            if family not in families:
                group.pop(family, None)
    return result
