"""API adapter factory."""

from __future__ import annotations

from crabcode_core.api.base import APIAdapter
from crabcode_core.types.config import ApiConfig


def create_adapter(config: ApiConfig) -> APIAdapter:
    """Create an API adapter based on configuration."""
    provider = config.provider or "anthropic"

    if provider == "anthropic":
        from crabcode_core.api.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(config)
    elif provider == "bedrock":
        from crabcode_core.api.anthropic_adapter import BedrockAdapter
        return BedrockAdapter(config)
    elif provider == "vertex":
        from crabcode_core.api.anthropic_adapter import VertexAdapter
        return VertexAdapter(config)
    elif provider == "openai":
        from crabcode_core.api.openai_adapter import OpenAIAdapter
        return OpenAIAdapter(config)
    elif provider == "azure":
        from crabcode_core.api.azure_adapter import AzureOpenAIAdapter
        return AzureOpenAIAdapter(config)
    elif provider == "gemini":
        from crabcode_core.api.gemini_adapter import GeminiAdapter
        return GeminiAdapter(config)
    elif provider == "ollama":
        from crabcode_core.api.ollama_adapter import OllamaAdapter
        return OllamaAdapter(config)
    elif provider == "codex":
        from crabcode_core.api.codex_adapter import CodexAdapter
        return CodexAdapter(config)
    elif provider == "router":
        fmt = config.format or "openai"
        if fmt == "anthropic":
            from crabcode_core.api.anthropic_adapter import AnthropicAdapter
            return AnthropicAdapter(config)
        elif fmt == "codex":
            from crabcode_core.api.codex_adapter import CodexAdapter
            return CodexAdapter(config)
        elif fmt == "ollama":
            from crabcode_core.api.ollama_adapter import OllamaAdapter
            return OllamaAdapter(config)
        elif fmt == "gemini":
            from crabcode_core.api.gemini_adapter import GeminiAdapter
            return GeminiAdapter(config)
        elif fmt == "azure":
            from crabcode_core.api.azure_adapter import AzureOpenAIAdapter
            return AzureOpenAIAdapter(config)
        else:
            from crabcode_core.api.openai_adapter import OpenAIAdapter
            return OpenAIAdapter(config)
    else:
        raise ValueError(f"Unknown API provider: {provider}")
