"""
Configuration for LLM service using fastapi-poe
Handles settings migration from .env to Django local_settings
"""

import os
from typing import Optional

# Default configuration values
DEFAULT_BOT_NAME = "Claude-3.7-Sonnet"
DEFAULT_SLEEP_TIME = 2.5
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 1


class LLMConfig:
    """Configuration manager for LLM service"""

    def __init__(self):
        self.api_key: Optional[str] = None
        self.bot_name: str = DEFAULT_BOT_NAME
        self.sleep_time: float = DEFAULT_SLEEP_TIME
        self.timeout: int = DEFAULT_TIMEOUT
        self.max_retries: int = DEFAULT_MAX_RETRIES

        # Try to load from Django settings first, then environment
        self._load_config()

    def _load_config(self):
        """Load configuration from Django settings or environment variables"""

        # Try Django settings first
        try:
            from django.conf import settings

            self.api_key = getattr(settings, "POE_API_KEY", None)
            self.bot_name = getattr(settings, "POE_BOT_NAME", DEFAULT_BOT_NAME)
            self.sleep_time = getattr(settings, "POE_SLEEP_TIME", DEFAULT_SLEEP_TIME)
            self.timeout = getattr(settings, "POE_TIMEOUT", DEFAULT_TIMEOUT)
            self.max_retries = getattr(settings, "POE_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        except ImportError:
            # Django not available, use environment variables
            pass

        # Fallback to environment variables
        if not self.api_key:
            self.api_key = os.getenv("POE_API_KEY") or os.getenv("API_KEY")

        if not self.bot_name or self.bot_name == DEFAULT_BOT_NAME:
            self.bot_name = os.getenv("POE_BOT_NAME", DEFAULT_BOT_NAME)

        if self.sleep_time == DEFAULT_SLEEP_TIME:
            try:
                self.sleep_time = float(
                    os.getenv(
                        "POE_SLEEP_TIME", os.getenv("SLEEP_TIME", DEFAULT_SLEEP_TIME)
                    )
                )
            except (ValueError, TypeError):
                self.sleep_time = DEFAULT_SLEEP_TIME

        if self.timeout == DEFAULT_TIMEOUT:
            try:
                self.timeout = int(os.getenv("POE_TIMEOUT", DEFAULT_TIMEOUT))
            except (ValueError, TypeError):
                self.timeout = DEFAULT_TIMEOUT

        if self.max_retries == DEFAULT_MAX_RETRIES:
            try:
                self.max_retries = int(
                    os.getenv("POE_MAX_RETRIES", DEFAULT_MAX_RETRIES)
                )
            except (ValueError, TypeError):
                self.max_retries = DEFAULT_MAX_RETRIES

    def validate(self) -> bool:
        """Validate that required configuration is present"""
        if not self.api_key:
            raise ValueError(
                "POE_API_KEY is required but not found in settings or environment"
            )
        return True

    def get_config_dict(self) -> dict:
        """Get configuration as dictionary"""
        return {
            "api_key": self.api_key,
            "bot_name": self.bot_name,
            "sleep_time": self.sleep_time,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }


# Global config instance
_config = None


def get_config() -> LLMConfig:
    """Get global config instance"""
    global _config
    if _config is None:
        _config = LLMConfig()
        _config.validate()
    return _config
