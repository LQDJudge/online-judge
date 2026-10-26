"""
General LLM API service for Poe API calls
Provides clean interface for various LLM tasks with image support
"""

import asyncio
import logging
import math
import os
import re
import signal
import time
from typing import List, Optional
from urllib.parse import unquote, urlparse

try:
    from django.apps import apps
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.files.storage import default_storage
except Exception:
    apps = None
    settings = None
    get_user_model = None
    default_storage = None

import fastapi_poe as fp
from fastapi_poe.client import PROTOCOL_VERSION
import httpx
import requests

from llm_service.errors import (
    LLMRequestCancelled,
    LLMRequestError as LLMRequestError,
    LLMRequestTimeout,
    classify_provider_error,
)

logger = logging.getLogger(__name__)


class LLMService:
    """General service for interacting with Poe API for various LLM tasks"""

    def __init__(
        self,
        api_key: str,
        bot_name: str = "Claude-Sonnet-4.6",
        sleep_time: float = 2.5,
        timeout: int = 120,
        feature: str = "",
        user=None,
        user_id: Optional[int] = None,
        metadata: Optional[dict] = None,
        parameters: Optional[dict] = None,
        transport_attempts: Optional[int] = None,
        streaming_timeouts: Optional[dict] = None,
        raise_errors: bool = False,
        should_continue=None,
        cancellation_poll_interval: float = 2,
    ):
        self.api_key = api_key
        self.bot_name = bot_name
        self.sleep_time = sleep_time
        self.timeout = timeout
        self.feature = feature
        self.user = user
        self.user_id = user_id
        self.metadata = metadata or {}
        self.parameters = dict(parameters or {})
        if transport_attempts is not None and (
            isinstance(transport_attempts, bool)
            or not isinstance(transport_attempts, int)
            or transport_attempts < 1
        ):
            raise ValueError("transport_attempts must be a positive integer")
        self.transport_attempts = transport_attempts
        self.streaming_timeouts = dict(streaming_timeouts or {})
        self.raise_errors = raise_errors
        self.last_error = None
        self.should_continue = should_continue
        if cancellation_poll_interval <= 0 or not math.isfinite(
            cancellation_poll_interval
        ):
            raise ValueError("Cancellation polling interval must be positive")
        self.cancellation_poll_interval = cancellation_poll_interval
        if self.streaming_timeouts:
            for key in ("connect", "first_output", "idle", "total"):
                value = self.streaming_timeouts.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError("Streaming timeout %s must be positive" % key)
            if transport_attempts != 1:
                raise ValueError(
                    "Streaming deadlines require one explicit transport attempt"
                )

        if not self.api_key:
            raise ValueError("API_KEY is required")

    def _resolve_usage_user(self):
        if self.user is not None:
            return self.user
        if not self.user_id:
            return None
        try:
            return get_user_model().objects.filter(id=self.user_id).first()
        except Exception:
            return None

    def retry_delay(self, default=2.5, max_wait=30):
        """Bound synchronous consumer retries; long rate limits return to UI."""
        if self.last_error:
            if not self.last_error.retryable:
                return None
            delay = max(default, self.last_error.retry_after or 0)
            return delay if delay <= max_wait else None
        return default

    def _message_stats(self, messages):
        input_chars = 0
        attachment_count = 0
        for message in messages:
            input_chars += len(getattr(message, "content", "") or "")
            attachment_count += len(getattr(message, "attachments", None) or [])
        return input_chars, attachment_count

    def _record_usage(
        self,
        messages,
        tools,
        started_at,
        response="",
        status="success",
        error="",
    ):
        try:
            if apps is None or not apps.ready:
                return
            AIUsageLog = apps.get_model("judge", "AIUsageLog")
            user = self._resolve_usage_user()
            input_chars, attachment_count = self._message_stats(messages)
            username = getattr(user, "username", "") if user is not None else ""
            AIUsageLog.objects.create(
                user=user,
                username=username,
                feature=self.feature or "",
                bot_name=self.bot_name or "",
                status=status,
                duration_ms=(time.monotonic() - started_at) * 1000,
                input_chars=input_chars,
                output_chars=len(response or ""),
                message_count=len(messages),
                attachment_count=attachment_count,
                tool_count=len(tools or []),
                error=(error or "")[:2000],
                metadata={
                    **self.metadata,
                    **(
                        {
                            "error_category": self.last_error.error_category,
                            "retryable": self.last_error.retryable,
                            "retry_after": self.last_error.retry_after,
                        }
                        if self.last_error
                        else {}
                    ),
                },
            )
        except Exception:
            logger.exception("Failed to record AI usage log")

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
        stream_timeout = (
            timeout if timeout is not None else getattr(self, "timeout", 120)
        )
        started_at = time.monotonic()
        self.last_error = None

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
            if stream_timeout > 0 and not self.streaming_timeouts:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(stream_timeout)

            try:
                stream = (
                    self._limited_transport_response(kwargs)
                    if self.transport_attempts is not None
                    else fp.get_bot_response_sync(**kwargs)
                )
                for partial in stream:
                    if partial.is_replace_response:
                        response = partial.text
                    else:
                        response += partial.text
                    logger.debug(f"LLM partial response: {partial.text}")
                    if on_partial:
                        on_partial(response)
            finally:
                if stream_timeout > 0 and not self.streaming_timeouts:
                    signal.alarm(0)
                    if old_handler is not None:
                        signal.signal(signal.SIGALRM, old_handler)

            if strip_thinking:
                cleaned = self._remove_thinking_content(response)
                if not cleaned and response:
                    logger.warning(
                        "strip_thinking removed entire response (%d chars). "
                        "First 500 chars: %s ... Last 500 chars: %s",
                        len(response),
                        response[:500],
                        response[-500:],
                    )
                response = cleaned
            response = response.strip()
            status = "success" if response else "error"
            error = "" if response else "empty response"
            self._record_usage(
                messages,
                tools,
                started_at,
                response=response,
                status=status,
                error=error,
            )
            return response

        except (TimeoutError, LLMRequestTimeout, httpx.TimeoutException) as e:
            if not isinstance(e, LLMRequestTimeout):
                kind = "connect" if isinstance(e, httpx.ConnectTimeout) else "total"
                seconds = self.streaming_timeouts.get(kind, stream_timeout)
                e = LLMRequestTimeout(kind, seconds)
            logger.error(f"LLM streaming timeout: {e}")
            self.last_error = e
            self._record_usage(
                messages,
                tools,
                started_at,
                response=response,
                status="timeout",
                error=str(e),
            )
            if self.raise_errors:
                e.raw_response = response
                raise e
            return None

        except Exception as e:
            error = classify_provider_error(e)
            error.raw_response = response
            self.last_error = error
            logger.error("LLM request failed: %s", error)
            self._record_usage(
                messages,
                tools,
                started_at,
                response=response,
                status="error",
                error=str(error),
            )
            if self.raise_errors:
                if error is e:
                    raise
                raise error from e
            return None

    def _limited_transport_response(self, kwargs):
        """The SDK convenience wrapper hides num_tries; use its public stream API."""
        request = fp.QueryRequest(
            query=kwargs["messages"],
            user_id="",
            conversation_id="",
            message_id="",
            version=PROTOCOL_VERSION,
            type="query",
        )
        stream_kwargs = {
            key: value for key, value in kwargs.items() if key != "messages"
        }
        if self.streaming_timeouts:
            yield from self._deadline_transport_response(request, stream_kwargs)
            return
        generator = fp.stream_request(
            request=request, num_tries=self.transport_attempts, **stream_kwargs
        ).__aiter__()
        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    yield loop.run_until_complete(generator.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(generator.aclose())
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def _deadline_transport_response(self, request, stream_kwargs):
        """Bound meaningful-output silence and total duration independently.

        SSE keepalives/empty events do not reset the output deadline. Use an
        owned async client so cancellation closes the connection, with no SDK
        retries hidden inside the coordinator's persisted attempt budget.
        """
        limits = self.streaming_timeouts

        async def responses():
            last_check = None

            async def check_cancelled():
                nonlocal last_check
                now = time.monotonic()
                if self.should_continue is None or (
                    last_check is not None
                    and now - last_check < self.cancellation_poll_interval
                ):
                    return
                last_check = now
                if not await asyncio.to_thread(self.should_continue):
                    raise LLMRequestCancelled(
                        "The AI request was cancelled or its execution claim expired"
                    )

            await check_cancelled()

            async def require_success(response):
                # The SDK's SSE reader checks content type, not HTTP status.
                # Preserve structured 401/402/429/5xx before SSE parsing wraps
                # them as an opaque "expected text/event-stream" failure.
                response.raise_for_status()

            timeout = httpx.Timeout(
                connect=limits["connect"],
                read=None,
                write=limits["connect"],
                pool=limits["connect"],
            )
            async with httpx.AsyncClient(
                timeout=timeout, event_hooks={"response": [require_success]}
            ) as session:
                stream = fp.stream_request(
                    request=request, session=session, num_tries=1, **stream_kwargs
                ).__aiter__()
                started = last_output = time.monotonic()
                received_output = False
                pending = None
                try:
                    while True:
                        now = time.monotonic()
                        kind = "idle" if received_output else "first_output"
                        remaining = limits[kind] - (now - last_output)
                        total_remaining = limits["total"] - (now - started)
                        if total_remaining <= remaining:
                            kind, remaining = "total", total_remaining
                        if remaining <= 0:
                            raise LLMRequestTimeout(kind, limits[kind])
                        try:
                            pending = asyncio.create_task(stream.__anext__())
                            deadline = time.monotonic() + remaining
                            while True:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise LLMRequestTimeout(kind, limits[kind])
                                wait = (
                                    min(remaining, self.cancellation_poll_interval)
                                    if self.should_continue
                                    else remaining
                                )
                                done, _ = await asyncio.wait({pending}, timeout=wait)
                                await check_cancelled()
                                if done:
                                    partial = pending.result()
                                    pending = None
                                    break
                        except StopAsyncIteration:
                            return
                        except asyncio.TimeoutError as exc:
                            raise LLMRequestTimeout(kind, limits[kind]) from exc
                        except Exception as exc:
                            # Poe wraps HTTPX failures in BotError. Inspect only
                            # the exception chain, never provider error text.
                            cause = exc
                            for _ in range(8):
                                if isinstance(cause, httpx.ConnectTimeout):
                                    raise LLMRequestTimeout(
                                        "connect", limits["connect"]
                                    ) from exc
                                if cause.__cause__ is None:
                                    break
                                cause = cause.__cause__
                            raise
                        if partial.text:
                            received_output = True
                            last_output = time.monotonic()
                        yield partial
                finally:
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await stream.aclose()

        loop = asyncio.new_event_loop()
        generator = responses()
        try:
            while True:
                try:
                    yield loop.run_until_complete(generator.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            try:
                loop.run_until_complete(generator.aclose())
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

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
                parameters=dict(self.parameters),
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
                parameters=dict(self.parameters),
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
        timeout: Optional[int] = None,
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
            timeout: Optional timeout in seconds (overrides default)

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
            timeout=timeout,
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

    def _is_supported_upload_file(self, file_name: str) -> bool:
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
        file_ext = os.path.splitext(file_name)[1].lower()
        if file_ext not in supported_extensions:
            logger.warning(
                f"Unsupported file extension: {file_ext} for file: {file_name}"
            )
            return False
        return True

    def _upload_bytes_to_poe(
        self, file_content: bytes, file_name: str, source_label: str
    ) -> Optional[fp.Attachment]:
        max_size = 50 * 1024 * 1024
        if len(file_content) > max_size:
            logger.warning(
                f"File too large ({len(file_content)} bytes): {source_label}"
            )
            return None

        if not self._is_supported_upload_file(file_name):
            return None

        logger.info(f"Uploading file to Poe: {source_label}")
        attachment = fp.upload_file_sync(
            file=file_content, file_name=file_name, api_key=self.api_key
        )
        logger.info(f"Successfully uploaded file: {file_name}")
        return attachment

    def _upload_problem_data_file(self, clean_path: str) -> Optional[fp.Attachment]:
        if settings is None:
            logger.error("Django not available for settings lookup")
            return None

        problem_data_root = getattr(settings, "DMOJ_PROBLEM_DATA_ROOT", None)
        if not problem_data_root:
            logger.error("DMOJ_PROBLEM_DATA_ROOT not found in Django settings")
            return None

        match = re.match(r"problem/([^/]+)/data/(.+)$", clean_path)
        if not match:
            logger.warning(f"Invalid problem data URL format: {clean_path}")
            return None

        problem_code, filename = match.groups()
        problem_path = self._clean_relative_storage_path(f"{problem_code}/{filename}")
        if not problem_path:
            logger.warning(f"Unsafe problem data URL path: {clean_path}")
            return None

        full_path = os.path.join(problem_data_root, problem_path)
        logger.info(f"Using problem data storage: {full_path}")

        if not os.path.exists(full_path):
            logger.warning(f"Local file not found: {full_path}")
            return None

        with open(full_path, "rb") as f:
            file_content = f.read()

        return self._upload_bytes_to_poe(
            file_content=file_content,
            file_name=os.path.basename(full_path),
            source_label=full_path,
        )

    def _get_media_storage_path(self, clean_path: str) -> str:
        if clean_path.startswith("media/"):
            return clean_path[6:]
        return clean_path

    def _clean_relative_storage_path(self, storage_path: str) -> Optional[str]:
        path = storage_path.replace("\\", "/")
        if path.startswith("/"):
            return None

        parts = [part for part in path.split("/") if part]
        if not parts or any(part in (".", "..") for part in parts):
            return None
        return "/".join(parts)

    def _upload_media_file(self, clean_path: str) -> Optional[fp.Attachment]:
        if settings is None or default_storage is None:
            logger.error("Django not available for media storage lookup")
            return None

        media_root = getattr(settings, "MEDIA_ROOT", None)
        storage_path = self._get_media_storage_path(clean_path)
        storage_path = self._clean_relative_storage_path(storage_path)
        if not storage_path:
            logger.warning(f"Unsafe media URL path: {clean_path}")
            return None

        if media_root:
            full_path = os.path.join(media_root, storage_path)
            logger.info(f"Using media filesystem storage: {full_path}")

            if not os.path.exists(full_path):
                logger.warning(f"Local file not found: {full_path}")
                return None

            with open(full_path, "rb") as f:
                file_content = f.read()

            return self._upload_bytes_to_poe(
                file_content=file_content,
                file_name=os.path.basename(full_path),
                source_label=full_path,
            )

        logger.info(f"Using Django default storage for media: {storage_path}")
        if not default_storage.exists(storage_path):
            logger.warning(f"Media file not found in default storage: {storage_path}")
            return None

        with default_storage.open(storage_path, "rb") as f:
            file_content = f.read()

        return self._upload_bytes_to_poe(
            file_content=file_content,
            file_name=os.path.basename(storage_path),
            source_label=storage_path,
        )

    def _upload_local_file(self, file_path: str) -> Optional[fp.Attachment]:
        """
        Upload a site-local file reference to Poe.

        Problem data PDFs are read from DMOJ_PROBLEM_DATA_ROOT. Media files are
        read from MEDIA_ROOT when configured, otherwise from Django default
        storage so S3-backed production media works without a local media tree.
        """
        try:
            clean_path = unquote(file_path).lstrip("/")
            if clean_path.startswith("problem/") and "/data/" in clean_path:
                return self._upload_problem_data_file(clean_path)
            return self._upload_media_file(clean_path)
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
