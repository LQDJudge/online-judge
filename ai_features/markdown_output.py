"""Conservative cleanup of generated Markdown without changing fenced contents."""

import re


def close_unclosed_fence(markdown: str) -> str:
    """Close only an unmatched line fence, retaining its kind and indentation."""
    opened = None
    for line in markdown.splitlines():
        if opened is None:
            match = re.match(r"^([ \t]*)(`{3,}|~{3,})(.*)$", line)
            if match and not (match[2].startswith("`") and "`" in match[3]):
                opened = (match[1], match[2])
        else:
            indentation, fence = opened
            if re.fullmatch(
                re.escape(indentation) + fence[0] + "{" + str(len(fence)) + r",}\s*",
                line,
            ):
                opened = None
    if opened:
        indentation, fence = opened
        markdown = markdown.rstrip("\r\n") + "\n" + indentation + fence
    return markdown


def clean_markdown_response(response: str) -> str:
    """Remove an explicit whole-response wrapper, never a sample closing fence."""
    if not response.strip():
        return ""
    lines = response.strip("\r\n").splitlines()
    if len(lines) >= 2:
        opening = re.fullmatch(
            r"(`{3,}|~{3,})(?:markdown|md)\s*", lines[0], re.IGNORECASE
        )
        if (
            opening
            and lines[-1].strip() == opening[1]
            and not lines[-1].startswith((" ", "\t"))
        ):
            lines = lines[1:-1]
    return close_unclosed_fence("\n".join(lines).strip("\r\n"))
