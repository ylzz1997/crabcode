"""API adapter layer - abstracts multiple LLM backends."""

from crabcode_core.api.base import APIAdapter, StreamChunk, ModelConfig
from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window
from crabcode_core.api.registry import create_adapter
from crabcode_core.api.ollama_adapter import OllamaAdapter
from crabcode_core.api.gemini_adapter import GeminiAdapter
from crabcode_core.api.azure_adapter import AzureOpenAIAdapter

__all__ = [
    "APIAdapter",
    "StreamChunk",
    "ModelConfig",
    "create_adapter",
    "DEFAULT_CONTEXT_WINDOW",
    "lookup_context_window",
    "OllamaAdapter",
    "GeminiAdapter",
    "AzureOpenAIAdapter",
]
