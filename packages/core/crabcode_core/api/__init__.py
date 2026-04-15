"""API adapter layer - abstracts multiple LLM backends."""

from crabcode_core.api.base import APIAdapter, StreamChunk, ModelConfig
from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window
from crabcode_core.api.registry import create_adapter

__all__ = [
    "APIAdapter",
    "StreamChunk",
    "ModelConfig",
    "create_adapter",
    "DEFAULT_CONTEXT_WINDOW",
    "lookup_context_window",
]
