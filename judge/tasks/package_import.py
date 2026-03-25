"""
Celery task for AI-powered problem package import.
Uploads zip to Poe, calls Claude Code, captures returned files and summary.
"""

import logging
import os
import time

import fastapi_poe as fp
from celery import shared_task

from judge.package_import.parser import (
    collect_response,
    download_returned_files,
    get_import_result,
)
from judge.package_import.prompts import build_import_prompt

logger = logging.getLogger(__name__)

# Claude Code bot name on Poe
CLAUDE_CODE_BOT = "Claude-Code"

# Max zip size: 50MB (Poe upload limit)
MAX_ZIP_SIZE = 50 * 1024 * 1024


@shared_task(bind=True)
def package_import_task(self, problem_code, zip_file_path):
    """
    Celery task to analyze a problem package using Claude Code on Poe.

    Uploads the zip to Poe CDN, sends it to Claude Code with the import prompt,
    captures returned files (description, testdata, checker, generator, solutions),
    and stores them in a temp directory for the Import page to display.

    Args:
        problem_code: The target problem code
        zip_file_path: Path to the uploaded zip file on disk

    Returns:
        Dict with import results:
        {
            "success": bool,
            "problem_code": str,
            "summary": dict,        # Parsed summary.json
            "files": dict,          # File inventory by category
            "save_dir": str,        # Temp dir with saved files
            "all_files": list,      # All saved file info dicts
            "error": str or None,
        }
    """
    save_dir = None
    try:
        from llm_service.config import get_config

        # Validate zip file
        if not os.path.exists(zip_file_path):
            return {"success": False, "error": "Zip file not found"}

        file_size = os.path.getsize(zip_file_path)
        if file_size > MAX_ZIP_SIZE:
            return {
                "success": False,
                "error": f"File too large ({file_size // (1024*1024)} MB). "
                f"Maximum is {MAX_ZIP_SIZE // (1024*1024)} MB.",
            }

        # Read and upload zip to Poe CDN
        config = get_config()
        with open(zip_file_path, "rb") as f:
            file_bytes = f.read()

        filename = os.path.basename(zip_file_path)
        logger.info(
            "Uploading %s (%d bytes) for problem %s",
            filename,
            len(file_bytes),
            problem_code,
        )

        attachment = fp.upload_file_sync(
            file=file_bytes,
            file_name=filename,
            api_key=config.api_key,
        )
        logger.info("Upload OK: %s", attachment.url)

        # Build prompt and send to Claude Code
        prompt = build_import_prompt()

        messages = [
            fp.ProtocolMessage(
                role="user",
                content=prompt,
                timestamp=int(time.time()),
                attachments=[attachment],
            )
        ]

        # Try up to 2 attempts (Claude Code sometimes creates files but forgets to attach them)
        max_attempts = 2
        raw_text = ""
        returned_files = []

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Sending to %s for analysis (attempt %d/%d)...",
                CLAUDE_CODE_BOT,
                attempt,
                max_attempts,
            )

            partials = fp.get_bot_response_sync(
                messages=messages,
                bot_name=CLAUDE_CODE_BOT,
                api_key=config.api_key,
            )
            raw_text, returned_files = collect_response(partials)

            logger.info(
                "Attempt %d: %d chars text, %d files returned",
                attempt,
                len(raw_text),
                len(returned_files),
            )

            if returned_files:
                break

            if attempt < max_attempts:
                logger.warning("No files returned on attempt %d, retrying...", attempt)

        if not returned_files:
            return {
                "success": False,
                "error": "Claude Code did not return any files. "
                "The processing may have failed or timed out. "
                "Please try uploading again.",
            }

        # Download files to temp directory
        save_dir, saved_files = download_returned_files(returned_files)

        logger.info(
            "Downloaded %d files to %s",
            len(saved_files),
            save_dir,
        )

        # Build import result
        result = get_import_result(save_dir, saved_files, raw_text)

        return {
            "success": True,
            "problem_code": problem_code,
            **result,
        }

    except Exception as e:
        logger.error(
            "Package import error for %s: %s",
            problem_code,
            e,
            exc_info=True,
        )
        return {
            "success": False,
            "error": str(e),
        }

    finally:
        # Clean up the uploaded zip file (but keep save_dir for the Import page)
        if os.path.exists(zip_file_path):
            try:
                os.unlink(zip_file_path)
            except OSError:
                pass
