"""
Preprocessor that auto-inserts blank lines around block math ($$...$$).

Why: pymdownx.arithmatex only treats $$...$$ as a block when it sits in its
own paragraph, i.e. has blank lines above and below. With the `nl2br` extension
also active, a line of $$ adjacent to text gets glued together and arithmatex
falls back to inline rendering.

This preprocessor runs BEFORE arithmatex and pads any line that is wholly a
block math expression so users no longer need to remember the blank-line rule.

Patterns padded:
  - A whole line of the form `$$...$$` (single-line block).
  - A standalone `$$` opening line and its matching `$$` closing line
    (multi-line block).
"""

import re

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor

_SINGLE_LINE_RE = re.compile(r"^\s*\$\$.+\$\$\s*$")
_DELIM_ONLY_RE = re.compile(r"^\s*\$\$\s*$")


class _BlockMathPaddingPreprocessor(Preprocessor):
    def run(self, lines):
        out = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]

            if _SINGLE_LINE_RE.match(line):
                if out and out[-1].strip() != "":
                    out.append("")
                out.append(line)
                if i + 1 < n and lines[i + 1].strip() != "":
                    out.append("")
                i += 1
                continue

            if _DELIM_ONLY_RE.match(line):
                # Look for matching close on a later line
                close = -1
                for j in range(i + 1, n):
                    if _DELIM_ONLY_RE.match(lines[j]):
                        close = j
                        break
                if close != -1:
                    if out and out[-1].strip() != "":
                        out.append("")
                    out.extend(lines[i : close + 1])
                    if close + 1 < n and lines[close + 1].strip() != "":
                        out.append("")
                    i = close + 1
                    continue

            out.append(line)
            i += 1

        return out


class BlockMathPaddingExtension(Extension):
    def extendMarkdown(self, md):
        # Priority must beat pymdownx.arithmatex's block preprocessor (priority 30)
        # so we run BEFORE it sees the lines.
        md.preprocessors.register(
            _BlockMathPaddingPreprocessor(md),
            "block_math_padding",
            32,
        )
