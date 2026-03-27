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
        self,
        api_key: str,
        bot_name: str = "Claude-Sonnet-4.6",
        sleep_time: float = 2.5,
        timeout: int = 120,
    ):
        self.api_key = api_key
        self.bot_name = bot_name
        self.sleep_time = sleep_time
        self.timeout = timeout

        if not self.api_key:
            raise ValueError("API_KEY is required")

    def _get_response(
        self,
        messages: List[fp.ProtocolMessage],
        tools: Optional[List["fp.ToolDefinition"]] = None,
        tool_executables: Optional[List] = None,
        strip_thinking: bool = True,
        timeout: Optional[int] = None,
        on_partial: Optional[callable] = None,
    ) -> Optional[str]:
        """
        Get a text response from the Poe API using the given messages.

        Args:
            timeout: Maximum seconds to wait for the streaming response.
                     If None, uses self.timeout (from config). Set 0 to disable.
        """
        import signal

        stream_timeout = (
            timeout if timeout is not None else getattr(self, "timeout", 120)
        )

        def _timeout_handler(signum, frame):
            raise TimeoutError(f"LLM streaming timed out after {stream_timeout}s")

        try:
            response = ""

            kwargs = {
                "messages": messages,
                "bot_name": self.bot_name,
                "api_key": self.api_key,
            }
            if tools:
                kwargs["tools"] = tools
            if tool_executables:
                kwargs["tool_executables"] = tool_executables

            # Set a timeout to prevent indefinite hangs during streaming
            old_handler = None
            if stream_timeout > 0:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(stream_timeout)

            try:
                for partial in fp.get_bot_response_sync(**kwargs):
                    if partial.is_replace_response:
                        response = partial.text
                    else:
                        response += partial.text
                    logger.debug(f"LLM partial response: {partial.text}")
                    if on_partial:
                        on_partial(response)
            finally:
                if stream_timeout > 0:
                    signal.alarm(0)
                    if old_handler is not None:
                        signal.signal(signal.SIGALRM, old_handler)

            if strip_thinking:
                response = self._remove_thinking_content(response)
            return response.strip()

        except TimeoutError as e:
            logger.error(f"LLM streaming timeout: {e}")
            return None
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

        Some models (e.g. Gemini) emit the full response twice:
          *Thinking...* > [thinking] answer *Thinking...* > [thinking] answer
        Splitting on *Thinking...* and taking the last clean segment handles this.
        """

        def _strip_thinking_lines(segment: str) -> str:
            result_lines = []
            for line in segment.split("\n"):
                stripped = line.strip()
                if stripped in ("*Thinking...*", "Thinking..."):
                    continue
                if stripped.startswith("Thinking...") and "elapsed" in stripped:
                    continue
                if stripped.startswith(">"):
                    continue
                if stripped == "":
                    if result_lines and result_lines[-1].strip() != "":
                        result_lines.append(line)
                    continue
                result_lines.append(line)
            return "\n".join(result_lines).strip()

        # Split on *Thinking...* markers — models like Gemini wrap the whole
        # response in repeated thinking+answer blocks; take the last clean segment
        import re

        segments = re.split(r"\*Thinking\.\.\.\*", text)
        clean_segments = [_strip_thinking_lines(s) for s in segments]
        non_empty = [s for s in clean_segments if s]

        if not non_empty:
            return ""

        return non_empty[-1]

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

    def _build_messages(
        self,
        conversation_messages: list,
        current_prompt: str,
        system_prompt: str = None,
    ) -> List[fp.ProtocolMessage]:
        """Build a ProtocolMessage list from conversation history."""
        messages = []

        if system_prompt:
            messages.append(
                fp.ProtocolMessage(
                    role="system",
                    content=system_prompt,
                    timestamp=int(time.time()),
                )
            )

        for msg in conversation_messages:
            role = msg["role"]
            if role not in ("user", "bot"):
                role = "bot" if role == "assistant" else role
            messages.append(
                fp.ProtocolMessage(
                    role=role,
                    content=msg["content"],
                    timestamp=msg.get("timestamp", int(time.time())),
                )
            )

        messages.append(
            fp.ProtocolMessage(
                role="user",
                content=current_prompt,
                timestamp=int(time.time()),
            )
        )

        return messages

    def call_llm_with_history(
        self,
        conversation_messages: list,
        current_prompt: str,
        system_prompt: str = None,
        tools: Optional[List["fp.ToolDefinition"]] = None,
        tool_executables: Optional[List] = None,
        strip_thinking: bool = True,
        on_partial: Optional[callable] = None,
    ) -> Optional[str]:
        """
        Call LLM with native message array for conversation history.

        Args:
            conversation_messages: List of dicts with 'role' and 'content' keys
            current_prompt: The current user prompt
            system_prompt: Optional system prompt
            tools: Optional list of ToolDefinition for native tool calling
            tool_executables: Optional list of callables for automatic tool execution
            strip_thinking: Whether to remove thinking content from response
            on_partial: Optional callback called with accumulated text during streaming

        Returns:
            LLM response as string, or None if failed
        """
        messages = self._build_messages(
            conversation_messages, current_prompt, system_prompt
        )
        return self._get_response(
            messages,
            tools=tools,
            tool_executables=tool_executables,
            strip_thinking=strip_thinking,
            on_partial=on_partial,
        )

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
            attachment = self.upload_file(url)
            if attachment:
                attachments.append(attachment)

        return attachments

    def upload_file(self, url: str) -> Optional[fp.Attachment]:
        """Upload a single file to Poe. Handles public URLs and local paths."""
        try:
            site_domain = self._get_site_domain()
            if url.startswith(("http://", "https://")):
                if self._is_site_url(url, site_domain):
                    local_path = self._extract_local_path_from_url(url)
                    if local_path:
                        attachment = self._upload_local_file(local_path)
                        if attachment:
                            return attachment
                return self._upload_file_from_url(url)
            elif url.startswith("/"):
                attachment = self._upload_local_file(url)
                if not attachment and url.startswith("/problem/") and "/data/" in url:
                    public_url = f"https://{site_domain or 'lqdoj.edu.vn'}{url}"
                    attachment = self._upload_file_from_url(public_url)
                return attachment
            else:
                logger.warning(f"Unsupported URL format: {url}")
                return None
        except Exception as e:
            logger.error(f"Error uploading file {url}: {e}")
            return None

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

            # Download and upload as bytes (more reliable than passing URL directly)
            logger.info(f"Downloading file from URL: {url}")
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            file_name = url.split("/")[-1] if "/" in url else f"file{file_ext}"
            attachment = fp.upload_file_sync(
                file=response.content, file_name=file_name, api_key=self.api_key
            )

            logger.info(f"Successfully uploaded file: {url}")
            return attachment

        except Exception as e:
            logger.error(f"Error uploading file from {url}: {e}")
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
