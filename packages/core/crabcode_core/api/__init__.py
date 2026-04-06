"""API adapter layer - abstracts multiple LLM backends."""

from crabcode_core.api.base import APIAdapter, StreamChunk, ModelConfig
from crabcode_core.api.registry import create_adapter

__all__ = ["APIAdapter", "StreamChunk", "ModelConfig", "create_adapter"]
