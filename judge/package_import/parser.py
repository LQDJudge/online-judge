"""
Parse Claude Code's response from Poe API.
Handles noisy streaming output, file attachment downloads, and summary parsing.
"""

import json
import logging
import os
import re
import tempfile

import requests

logger = logging.getLogger(__name__)

# Claude Code's internal files we should skip
SKIP_FILES = {".bot_memory.tar.gz", ".bot_memory"}


def collect_response(partials):
    """
    Collect text and file attachments from Claude Code's streaming response.

    Args:
        partials: Iterator of BotMessage from fp.get_bot_response_sync()

    Returns:
        (raw_text, returned_files) where returned_files is a list of
        {"name": str, "url": str, "content_type": str}
    """
    raw_text = ""
    returned_files = []

    for partial in partials:
        raw_text += partial.text
        if (
            hasattr(partial, "attachment")
            and partial.attachment
            and partial.attachment.url
        ):
            name = partial.attachment.name or "unknown"
            # Skip internal Claude Code files
            if any(name.startswith(skip) for skip in SKIP_FILES):
                logger.debug("Skipping internal file: %s", name)
                continue

            returned_files.append(
                {
                    "name": name,
                    "url": partial.attachment.url,
                    "content_type": partial.attachment.content_type or "unknown",
                }
            )
            logger.info("File received: %s (%s)", name, partial.attachment.content_type)

    logger.info(
        "Response collected: %d chars text, %d files",
        len(raw_text),
        len(returned_files),
    )

    if not returned_files and len(raw_text) > 0:
        # Log a snippet for debugging (not to disk)
        logger.warning(
            "No files returned. Last 500 chars of response: %s",
            raw_text[-500:],
        )

    return raw_text, returned_files


def download_returned_files(returned_files, save_dir=None):
    """
    Download file attachments from Poe CDN URLs to a local directory.

    Args:
        returned_files: List of {"name", "url", "content_type"} dicts
        save_dir: Directory to save files (created if None)

    Returns:
        (save_dir, saved_files) where saved_files is a list of
        {"name": str, "size": int, "path": str, "content_type": str}
    """
    if save_dir is None:
        save_dir = tempfile.mkdtemp(prefix="import_files_")

    saved_files = []
    for fi in returned_files:
        try:
            resp = requests.get(fi["url"], timeout=120)
            resp.raise_for_status()

            # Strip path prefixes (Claude Code uses /mnt/ sandbox paths)
            clean_name = os.path.basename(fi["name"])
            save_path = os.path.join(save_dir, clean_name)

            with open(save_path, "wb") as f:
                f.write(resp.content)

            saved_files.append(
                {
                    "name": clean_name,
                    "size": len(resp.content),
                    "path": save_path,
                    "content_type": fi["content_type"],
                }
            )
            logger.info("Saved: %s (%d bytes)", clean_name, len(resp.content))

        except Exception as e:
            logger.error("Failed to download %s: %s", fi["name"], e)

    return save_dir, saved_files


def parse_summary(save_dir):
    """
    Parse summary.json from the saved files directory.

    Returns:
        Parsed dict or None if summary.json not found/invalid.
    """
    summary_path = os.path.join(save_dir, "summary.json")
    if not os.path.exists(summary_path):
        logger.warning("summary.json not found in %s", save_dir)
        return None

    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to parse summary.json: %s", e)
        return None


def extract_summary_from_text(raw_text):
    """
    Fallback: extract summary JSON from Claude Code's noisy text response.
    Used when summary.json is not returned as a file attachment.

    Args:
        raw_text: The full raw streaming text from Claude Code

    Returns:
        Parsed dict or None
    """
    # Strategy 1: Find ```json ... ``` blocks
    json_blocks = re.findall(r"```json\s*\n(.*?)```", raw_text, re.DOTALL)
    if json_blocks:
        for block in reversed(json_blocks):
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue

    # Strategy 2: Find the last valid JSON object (limited search)
    last_brace = raw_text.rfind("}")
    if last_brace >= 0:
        search_start = max(last_brace - 50000, 0)  # Search at most 50KB back
        # Find only the opening braces (not every char)
        for start in range(last_brace, search_start, -1):
            if raw_text[start] == "{":
                try:
                    return json.loads(raw_text[start : last_brace + 1])
                except json.JSONDecodeError:
                    continue
                break  # Only try first few matches to avoid O(n²)

    return None


def get_import_result(save_dir, saved_files, raw_text=""):
    """
    Build the final import result dict from saved files and optional text.

    Args:
        save_dir: Directory containing saved files
        saved_files: List of saved file info dicts
        raw_text: Raw text response (fallback for summary)

    Returns:
        Dict with all import data ready for the Import page.
    """
    # Parse summary (prefer file, fall back to text)
    summary = parse_summary(save_dir)
    if summary is None and raw_text:
        summary = extract_summary_from_text(raw_text)

    # Build file inventory
    file_inventory = {}
    for f in saved_files:
        name = f["name"]
        if name == "summary.json":
            continue  # Already parsed
        elif name == "description.md":
            file_inventory["description"] = f
        elif name == "testdata.zip":
            file_inventory["testdata"] = f
        elif name == "checker.cpp":
            file_inventory["checker"] = f
        elif name == "generator.cpp":
            file_inventory["generator"] = f
        elif name == "generator_script.txt":
            file_inventory["generator_script"] = f
        elif name == "interactive.cpp":
            file_inventory["interactive"] = f
        elif name.startswith("sol_"):
            file_inventory.setdefault("solutions", []).append(f)
        elif any(
            name.lower().endswith(ext)
            for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
        ):
            file_inventory.setdefault("images", []).append(f)

    return {
        "summary": summary or {},
        "files": file_inventory,
        "save_dir": save_dir,
        "all_files": saved_files,
    }
