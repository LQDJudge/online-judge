"""
Celery task for AI-powered quiz question import.
Uploads file to Poe CDN, calls Gemini 3 Flash, parses JSON response.
"""

import logging
import os

import fastapi_poe as fp
from celery import shared_task

from ai_features.quiz_import_service import (
    QUIZ_IMPORT_SYSTEM_PROMPT,
    QUIZ_IMPORT_USER_PROMPT,
    parse_quiz_import_response,
)

logger = logging.getLogger(__name__)

GEMINI_FLASH_BOT = "Gemini-3-Flash"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


@shared_task(bind=True)
def quiz_import_task(self, file_path, user_id):
    """
    Celery task to extract quiz questions from an uploaded file using AI.

    Args:
        file_path: Path to the uploaded temp file
        user_id: ID of the user who initiated the import

    Returns:
        Dict with extraction results:
        {
            "success": bool,
            "questions": [...],
            "summary": {...},
            "error": str or None,
        }
    """
    try:
        from llm_service.config import get_config
        from llm_service.llm_api import LLMService

        if not os.path.exists(file_path):
            return {"success": False, "error": "Uploaded file not found"}

        file_size = os.path.getsize(file_path)
        if file_size > MAX_UPLOAD_SIZE:
            return {
                "success": False,
                "error": f"File too large ({file_size // (1024 * 1024)} MB). "
                f"Maximum is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
            }

        config = get_config()

        # Read and upload file to Poe CDN
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        filename = os.path.basename(file_path)
        logger.info(
            "Uploading %s (%d bytes) for quiz import (user %s)",
            filename,
            len(file_bytes),
            user_id,
        )

        attachment = fp.upload_file_sync(
            file=file_bytes,
            file_name=filename,
            api_key=config.api_key,
        )
        logger.info("Upload OK: %s", attachment.url)

        # Call Gemini 3 Flash with the document
        service = LLMService(
            api_key=config.api_key,
            bot_name=GEMINI_FLASH_BOT,
            timeout=300,
        )
        response_text = service.call_llm(
            prompt=QUIZ_IMPORT_USER_PROMPT,
            system_prompt=QUIZ_IMPORT_SYSTEM_PROMPT,
            attachments=[attachment],
        )

        if not response_text:
            return {
                "success": False,
                "error": "AI did not return a response. Please try again.",
            }

        logger.info(
            "Got %d chars response for quiz import (user %s)",
            len(response_text),
            user_id,
        )

        # Parse the response
        result = parse_quiz_import_response(response_text)
        return result

    except Exception as e:
        logger.error("Error in quiz_import_task for user %s: %s", user_id, e)
        return {"success": False, "error": str(e)}

    finally:
        # Clean up temp file
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
        except OSError:
            pass
