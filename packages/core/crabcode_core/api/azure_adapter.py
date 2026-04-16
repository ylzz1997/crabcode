"""Azure OpenAI API adapter — inherits from OpenAIAdapter with Azure-specific client."""

from __future__ import annotations

import os
from typing import Any

from crabcode_core.api.base import ModelConfig
from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.types.config import ApiConfig


class AzureOpenAIAdapter(OpenAIAdapter):
    """Adapter for Azure OpenAI endpoints.

    Azure OpenAI uses *deployment names* instead of model names.  The
    ``config.model`` value is treated as the Azure deployment name.

    Required environment variables (or equivalent config):
        - AZURE_OPENAI_API_KEY     (or config.api_key_env)
        - AZURE_OPENAI_ENDPOINT
        - AZURE_OPENAI_API_VERSION (defaults to ``2024-10-21``)
        - AZURE_OPENAI_DEPLOYMENT  (optional; overrides config.model)
    """

    def __init__(self, config: ApiConfig):
        # Azure deployment name: env override takes precedence
        self._deployment_override = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        super().__init__(config)

    def _create_client(self, config: ApiConfig) -> Any:
        """Create an AsyncAzureOpenAI client."""
        import openai

        # --- API key ---
        api_key: str | None = None
        if config.api_key_env:
            api_key = os.environ.get(config.api_key_env)
        if not api_key:
            api_key = os.environ.get("AZURE_OPENAI_API_KEY")

        # --- Azure endpoint ---
        azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not azure_endpoint and config.base_url:
            azure_endpoint = config.base_url

        # --- API version ---
        api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if azure_endpoint:
            kwargs["azure_endpoint"] = azure_endpoint
        kwargs["api_version"] = api_version

        return openai.AsyncAzureOpenAI(**kwargs)

    def _resolve_model(self, config: ModelConfig) -> str:
        """Azure uses deployment names instead of model names."""
        return (
            self._deployment_override
            or config.model
            or self.config.model
            or "gpt-4o"
        )
