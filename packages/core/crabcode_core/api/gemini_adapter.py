"""Gemini API adapter — uses Google's OpenAI-compatible endpoint."""

from __future__ import annotations

from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.types.config import ApiConfig


class GeminiAdapter(OpenAIAdapter):
    """Adapter for Google Gemini via its OpenAI-compatible endpoint.

    Google Gemini has provided an OpenAI-compatible endpoint since late 2024
    (``https://generativelanguage.googleapis.com/v1beta/openai/``), so we
    simply inherit from :class:`OpenAIAdapter` and pre-fill the base URL,
    API key environment variable, and disable thinking (not supported on the
    OpenAI-compatible surface).
    """

    def __init__(self, config: ApiConfig):
        if not config.base_url:
            config = config.model_copy(update={"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"})
        if not config.api_key_env:
            config = config.model_copy(update={"api_key_env": "GEMINI_API_KEY"})
        if config.thinking_enabled is True:
            config = config.model_copy(update={"thinking_enabled": False})
        super().__init__(config)
