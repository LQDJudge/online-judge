"""
General LLM API service for Poe API calls
Provides clean interface for various LLM tasks with image support
"""

import fastapi_poe as fp
import time
import re
import requests
import os
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


class LLMService:
    """General service for interacting with Poe API for various LLM tasks"""

    def __init__(
        self, api_key: str, bot_name: str = "Claude-3.7-Sonnet", sleep_time: float = 2.5
    ):
        self.api_key = api_key
        self.bot_name = bot_name
        self.sleep_time = sleep_time

        if not self.api_key:
            raise ValueError("API_KEY is required")

    def _get_response(self, messages: List[fp.ProtocolMessage]) -> Optional[str]:
        """
        Get a response from the Poe API using the given messages
        """
        try:
            response = ""

            for partial in fp.get_bot_response_sync(
                messages=messages,
                bot_name=self.bot_name,
                api_key=self.api_key,
            ):
                response += partial.text
                logger.debug(f"LLM partial response: {partial.text}")

            # Remove thinking content (blockquotes with > prefix)
            response = self._remove_thinking_content(response)
            return response.strip()

        except Exception as e:
            logger.error(f"Error during LLM API call: {e}")
            return None

    def _remove_thinking_content(self, text: str) -> str:
        """
        Remove extended thinking content from LLM response.
        Thinking content appears as:
        - Blockquotes with > prefix
        - *Thinking...* markers
        - Thinking... (Xs elapsed) markers
        """
        lines = text.split("\n")
        result_lines = []

        for line in lines:
            stripped = line.strip()
            # Skip various thinking markers
            if stripped in ("*Thinking...*", "Thinking..."):
                continue
            # Skip "Thinking... (Xs elapsed)" pattern
            if stripped.startswith("Thinking...") and "elapsed" in stripped:
                continue
            # Skip thinking block lines (starting with >)
            if stripped.startswith(">"):
                continue
            # Skip empty lines at the beginning or consecutive empty lines
            if stripped == "":
                if result_lines and result_lines[-1].strip() != "":
                    result_lines.append(line)
                continue
            # Non-thinking content - keep it
            result_lines.append(line)

        return "\n".join(result_lines)

    def call_llm(
        self,
        prompt: str,
        system_prompt: str = None,
        attachments: List[fp.Attachment] = None,
    ) -> Optional[str]:
        """
        General LLM function for any task

        Args:
            prompt: The user prompt to send to the LLM
            system_prompt: Optional system prompt to set context
            attachments: Optional list of attachments (images, files)

        Returns:
            LLM response as string, or None if failed
        """
        messages = []

        if system_prompt:
            messages.append(
                fp.ProtocolMessage(
                    role="system",
                    content=system_prompt,
                    timestamp=int(time.time()),
                )
            )

        messages.append(
            fp.ProtocolMessage(
                role="user",
                content=prompt,
                timestamp=int(time.time()),
                attachments=attachments or [],
            )
        )

        return self._get_response(messages)

    def _get_site_domain(self) -> Optional[str]:
        """Get the site domain from Django's Site model."""
        try:
            from django.contrib.sites.models import Site

            site = Site.objects.get_current()
            return site.domain if site else None
        except Exception:
            return None

    def _is_site_url(self, url: str, site_domain: str) -> bool:
        """Check if URL belongs to site domain or its subdomains."""
        if not site_domain:
            return False
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            url_domain = parsed.netloc.lower()
            site_domain = site_domain.lower()

            # Check if URL domain matches site domain or is a subdomain
            # e.g., cdn.lqdoj.edu.vn is subdomain of lqdoj.edu.vn
            return url_domain == site_domain or url_domain.endswith(f".{site_domain}")
        except Exception:
            return False

    def _extract_local_path_from_url(self, url: str) -> Optional[str]:
        """Extract local file path from a site URL."""
        try:
            from urllib.parse import urlparse, unquote

            parsed = urlparse(url)
            path = unquote(parsed.path)

            # Return path starting with /
            if path.startswith("/"):
                return path
            return f"/{path}"
        except Exception:
            return None

    def extract_and_upload_files(self, content: str) -> List[fp.Attachment]:
        """
        Extract file references from markdown content and upload them to Poe

        Args:
            content: Markdown content that may contain images or file links

        Returns:
            List of uploaded Attachment objects
        """
        attachments = []

        # Get site domain for checking local URLs
        site_domain = self._get_site_domain()

        # Find markdown file references
        # 1. Images: ![alt](url)
        image_pattern = r"!\[[^\]]*\]\(([^)]+)\)"
        # 2. Links: [text](url) - for PDFs and other files
        link_pattern = r"(?<!!)\[[^\]]*\]\(([^)]+)\)"
        # 3. Reference style: ![alt][ref] or [text][ref] ... [ref]: url
        ref_pattern = r"(?:!?)\[[^\]]*\]\[([^\]]+)\]"
        ref_def_pattern = r"^\s*\[([^\]]+)\]:\s*(\S+)"

        # Get all file URLs
        file_urls = []
        file_urls.extend(re.findall(image_pattern, content))
        file_urls.extend(re.findall(link_pattern, content))

        # Get reference-style files
        ref_files = re.findall(ref_pattern, content)
        if ref_files:
            ref_defs = re.findall(ref_def_pattern, content, re.MULTILINE)
            ref_map = {ref: url for ref, url in ref_defs}
            for ref in ref_files:
                if ref in ref_map:
                    file_urls.append(ref_map[ref])

        # Upload files to Poe
        for url in set(file_urls):  # Remove duplicates
            try:
                if url.startswith(("http://", "https://")):
                    # Check if this is a site URL (or subdomain like cdn.lqdoj.edu.vn)
                    if self._is_site_url(url, site_domain):
                        # Try to load from local files first
                        local_path = self._extract_local_path_from_url(url)
                        if local_path:
                            logger.info(
                                f"Site URL detected, trying local path: {local_path}"
                            )
                            attachment = self._upload_local_file(local_path)
                            if attachment:
                                attachments.append(attachment)
                                continue
                        # Fall through to URL upload if local fails

                    # Handle public URLs
                    attachment = self._upload_file_from_url(url)
                    if attachment:
                        attachments.append(attachment)
                elif url.startswith("/"):
                    # Handle local file paths - try local first, then public URL fallback
                    attachment = self._upload_local_file(url)
                    if not attachment:
                        # Try as public URL with domain prefix for PDFs
                        if url.startswith("/problem/") and "/data/" in url:
                            public_url = f"https://{site_domain or 'lqdoj.edu.vn'}{url}"
                            logger.info(
                                f"Local PDF not found, trying public URL: {public_url}"
                            )
                            attachment = self._upload_file_from_url(public_url)

                    if attachment:
                        attachments.append(attachment)
                else:
                    logger.warning(f"Unsupported URL format: {url}")

            except Exception as e:
                logger.error(f"Error uploading file from {url}: {e}")

        return attachments

    def _upload_file_from_url(self, url: str) -> Optional[fp.Attachment]:
        """
        Upload a file from URL to Poe using fp.upload_file_sync

        Args:
            url: Public URL to the file

        Returns:
            Uploaded Attachment object or None if failed
        """
        try:
            # Check if it's a supported file type
            supported_extensions = {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".bmp",
                ".webp",
                ".pdf",
                ".txt",
                ".md",
            }
            file_ext = "." + url.split(".")[-1].lower() if "." in url else ""

            if file_ext not in supported_extensions:
                logger.warning(f"Unsupported file extension: {file_ext} for URL: {url}")
                return None

            # Upload file from URL
            logger.info(f"Uploading file from URL: {url}")
            attachment = fp.upload_file_sync(file_url=url, api_key=self.api_key)

            logger.info(f"Successfully uploaded file: {url}")
            return attachment

        except Exception as e:
            logger.error(f"Error uploading file from {url}: {e}")
            # Fallback: try to download and upload as bytes
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()

                file_name = url.split("/")[-1] if "/" in url else f"file{file_ext}"
                attachment = fp.upload_file_sync(
                    file=response.content, file_name=file_name, api_key=self.api_key
                )

                logger.info(f"Successfully uploaded file via download: {url}")
                return attachment

            except Exception as e2:
                logger.error(f"Fallback upload also failed for {url}: {e2}")
                return None

    def _upload_local_file(self, file_path: str) -> Optional[fp.Attachment]:
        """
        Upload a local file to Poe using fp.upload_file_sync
        Handles both MEDIA_ROOT files and DMOJ_PROBLEM_DATA_ROOT files (for PDFs)

        Args:
            file_path: Local file path (URL path like /media/file.jpg or /problem/code/data/file.pdf)

        Returns:
            Uploaded Attachment object or None if failed
        """
        try:
            # Get Django settings
            try:
                from django.conf import settings

                media_root = getattr(settings, "MEDIA_ROOT", None)
                problem_data_root = getattr(settings, "DMOJ_PROBLEM_DATA_ROOT", None)
            except ImportError:
                logger.error("Django not available for settings lookup")
                return None

            # Determine storage type and construct full file path
            clean_path = file_path.lstrip("/")

            # Check if this is a problem data file (PDF)
            if clean_path.startswith("problem/") and "/data/" in clean_path:
                # This is a problem PDF: /problem/{code}/data/{filename}
                if not problem_data_root:
                    logger.error("DMOJ_PROBLEM_DATA_ROOT not found in Django settings")
                    return None

                # Extract problem code and filename from URL
                # Pattern: problem/{code}/data/{filename}
                import re

                match = re.match(r"problem/([^/]+)/data/(.+)$", clean_path)
                if not match:
                    logger.warning(f"Invalid problem data URL format: {clean_path}")
                    return None

                problem_code, filename = match.groups()
                # Problem data files are stored as: {DMOJ_PROBLEM_DATA_ROOT}/{code}/{filename}
                # Example: /problems/qnadrill/qnadrill.pdf
                full_path = os.path.join(problem_data_root, problem_code, filename)
                logger.info(f"Using problem data storage: {full_path}")

            else:
                # This is a regular media file (images, uploads, etc.)
                if not media_root:
                    logger.error("MEDIA_ROOT not found in Django settings")
                    return None

                # For media files, remove 'media/' prefix since MEDIA_ROOT already points to media directory
                # Example: /media/pagedown-uploads/image.png -> pagedown-uploads/image.png
                if clean_path.startswith("media/"):
                    clean_path = clean_path[6:]  # Remove 'media/' prefix

                full_path = os.path.join(media_root, clean_path)
                logger.info(f"Using media storage: {full_path}")

            # Check if file exists
            if not os.path.exists(full_path):
                logger.warning(f"Local file not found: {full_path}")
                return None

            # Check file size (optional safety check)
            file_size = os.path.getsize(full_path)
            max_size = 50 * 1024 * 1024  # 50MB limit
            if file_size > max_size:
                logger.warning(f"File too large ({file_size} bytes): {full_path}")
                return None

            # Check if it's a supported file type
            supported_extensions = {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".bmp",
                ".webp",
                ".pdf",
                ".txt",
                ".md",
            }
            file_ext = os.path.splitext(full_path)[1].lower()

            if file_ext not in supported_extensions:
                logger.warning(
                    f"Unsupported file extension: {file_ext} for file: {full_path}"
                )
                return None

            # Read file content
            with open(full_path, "rb") as f:
                file_content = f.read()

            # Extract file name
            file_name = os.path.basename(full_path)

            # Upload file to Poe
            logger.info(f"Uploading local file: {full_path}")
            attachment = fp.upload_file_sync(
                file=file_content, file_name=file_name, api_key=self.api_key
            )

            logger.info(f"Successfully uploaded local file: {file_name}")
            return attachment

        except Exception as e:
            logger.error(f"Error uploading local file {file_path}: {e}")
            return None

    def call_llm_with_files(
        self, prompt: str, content_with_files: str, system_prompt: str = None
    ) -> Optional[str]:
        """
        Call LLM with automatic file extraction and upload from markdown content

        Args:
            prompt: The user prompt
            content_with_files: Markdown content that may contain images, PDFs, or other files
            system_prompt: Optional system prompt

        Returns:
            LLM response as string, or None if failed
        """
        # Extract and upload files
        attachments = self.extract_and_upload_files(content_with_files)

        if attachments:
            logger.info(f"Successfully uploaded {len(attachments)} file(s) to Poe")
        else:
            logger.info("No files found or uploaded")

        # Combine prompt with content
        full_prompt = f"{prompt}\n\nCONTENT:\n{content_with_files}"

        return self.call_llm(full_prompt, system_prompt, attachments)
