"""Ollama API adapter — delegates to OpenAIAdapter with Ollama defaults."""

from __future__ import annotations

from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.types.config import ApiConfig


class OllamaAdapter(OpenAIAdapter):
    """Adapter for Ollama via its OpenAI-compatible Chat Completions API.

    Ollama natively exposes an OpenAI-compatible endpoint at
    ``http://localhost:11434/v1`` and does not require a real API key,
    but the OpenAI SDK still expects *some* value for ``api_key``.
    This adapter pre-fills those defaults and preserves reasoning controls
    for local models that support thinking.
    """

    def __init__(self, config: ApiConfig):
        # Pre-fill Ollama defaults before handing off to OpenAIAdapter
        if not config.base_url:
            config = config.model_copy(update={"base_url": "http://localhost:11434/v1"})
        if not config.api_key_env:
            config = config.model_copy(update={"api_key_env": "OLLAMA_API_KEY"})
        super().__init__(config)

    def _allows_placeholder_api_key(self) -> bool:
        return True
