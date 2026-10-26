"""Bounded, offline PDF text extraction; never execute embedded PDF actions.

Parsing runs in a disposable resource-limited process, not in a web/worker
process. Image/vector/scanned content is explicitly left for manual assessment.
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 16 * 1024 * 1024

EXTRACT_SCRIPT = r"""
import io
import json
import re
import resource
import sys
from pypdf import PdfReader

resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=True)
if reader.is_encrypted:
    raise ValueError("encrypted PDF")
if len(reader.pages) > 40:
    raise ValueError("page limit")
texts = []
visual = False
length = 0
for number, page in enumerate(reader.pages, 1):
    text = page.extract_text() or ""
    length += len(text)
    if length > 180000:
        raise ValueError("text limit")
    resources = page.get("/Resources", {})
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    # XObjects may contain diagrams or scanned pages. Drawing operators also
    # catch vector diagrams and outlined text not represented by extracted text.
    content = page.get_contents()
    raw = content.get_data() if content is not None else b""
    visual |= bool(resources.get("/XObject")) or bool(
        re.search(rb"(?:^|\s)(?:m|l|c|re|Do)(?:\s|$)", raw)
    ) or not text.strip()
    texts.append("[PDF page %d]\n%s" % (number, text))
print(json.dumps({"text": "\n\n".join(texts), "pages": len(reader.pages),
                  "requires_manual_review": visual or not bool(texts)}))
"""


def extract_pdf_statement(field):
    unavailable = {
        "text": "",
        "requires_manual_review": True,
        "limitation": "PDF text could not be safely extracted; do not infer missing rules.",
    }
    try:
        with field.storage.open(field.name, "rb") as source:
            data = source.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            return unavailable
        result = subprocess.run(
            [sys.executable, "-I", "-c", EXTRACT_SCRIPT],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            timeout=10,
            check=True,
        )
        parsed = json.loads(result.stdout)
        if not isinstance(parsed.get("text"), str) or len(parsed["text"]) > 185000:
            return unavailable
        return parsed
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("PDF statement extraction unavailable", exc_info=True)
        return unavailable


def review_statement_text(snapshot):
    description = snapshot.get("statement", "")
    pdf = snapshot.get("pdf_statement", {})
    if not pdf and not snapshot.get("artifacts", {}).get("pdf_description"):
        return description
    return "\n\n".join(
        part
        for part in (
            description,
            "[Uploaded PDF statement — extracted text; not Markdown source]\n"
            + (
                pdf.get("text")
                or "No usable PDF text available. Do not invent the problem statement."
            ),
        )
        if part
    )


def statement_requires_manual_reading(snapshot):
    return bool(
        not snapshot.get("statement", "").strip()
        and snapshot.get("artifacts", {}).get("pdf_description")
        and not snapshot.get("pdf_statement", {}).get("text", "").strip()
    )
