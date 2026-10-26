"""Decode model JSON; narrowly preserve literal LaTeX braces in review prose.

No code evaluation, general JSON repair, truncation recovery or executable-field
rewriting is allowed. Callers must still validate the full response schema.
"""

import json

NARRATIVE_FIELDS = frozenset(
    (
        "summary",
        "author_feedback",
        "claim",
        "reasoning",
        "suggested_probe_or_fix",
        "core_comparison",
        "substantive_differences",
        "algorithmic_significance",
        "limitations",
        "uncertainties",
        "rationale",
    )
)
MARKER = "\ue000REVIEW_LITERAL_BACKSLASH\ue001"


def _reject_nonfinite(value):
    raise ValueError("Nonfinite JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_review_json(text):
    try:
        return (
            json.loads(
                text, parse_constant=_reject_nonfinite, object_pairs_hook=_unique_object
            ),
            [],
        )
    except json.JSONDecodeError as error:
        if error.msg != "Invalid \\escape" or MARKER in text:
            raise

    # Preserve all valid escapes byte-for-byte. Only literal backslash-braces
    # INSIDE strings are candidates; malformed JSON outside strings still fails.
    fragments = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            fragments.append(
                MARKER + following if following in "{}" else char + following
            )
            index += 2
            continue
        if char == '"':
            in_string = not in_string
        fragments.append(char)
        index += 1
    output = json.loads(
        "".join(fragments),
        parse_constant=_reject_nonfinite,
        object_pairs_hook=_unique_object,
    )
    repairs = []

    def restore(value, field=None, path="$"):
        if isinstance(value, dict):
            for key, child in value.items():
                if MARKER in key:
                    raise ValueError("Cannot repair JSON keys")
                value[key] = restore(child, key, path + "." + key)
        elif isinstance(value, list):
            value = [
                restore(child, field, "%s[%s]" % (path, index))
                for index, child in enumerate(value)
            ]
        elif isinstance(value, str) and MARKER in value:
            if field not in NARRATIVE_FIELDS:
                raise ValueError("Invalid escape outside review prose")
            repairs.append(path)
            return value.replace(MARKER, "\\")
        return value

    return restore(output), repairs
