"""Abstract base for all API adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from crabcode_core.types.message import Message


def _raw_usage_field(raw: Any, *keys: str) -> tuple[Any, bool]:
    for key in keys:
        if isinstance(raw, dict):
            if key in raw and raw[key] is not None:
                return raw[key], True
        else:
            value = getattr(raw, key, None)
            if value is not None:
                return value, True
    return None, False


def usage_int_field(raw: Any, *keys: str) -> tuple[int, bool]:
    value, present = _raw_usage_field(raw, *keys)
    if not present:
        return 0, False
    try:
        return max(0, int(value)), True
    except (TypeError, ValueError):
        return 0, False


def normalize_openai_usage(raw: Any) -> dict[str, int]:
    """Normalize Chat Completions and Responses usage fields."""
    usage: dict[str, int] = {}
    input_tokens, has_input = usage_int_field(raw, "input_tokens", "prompt_tokens")
    output_tokens, has_output = usage_int_field(raw, "output_tokens", "completion_tokens")
    if has_input:
        usage["input_tokens"] = input_tokens
        usage["total_input_tokens"] = input_tokens
    if has_output:
        usage["output_tokens"] = output_tokens

    details, has_details = _raw_usage_field(
        raw,
        "input_tokens_details",
        "prompt_tokens_details",
    )
    if has_details:
        cached_tokens, has_cached = usage_int_field(details, "cached_tokens")
        if has_cached:
            usage["cache_read_tokens"] = cached_tokens
    return usage


@dataclass
class ModelConfig:
    """Configuration for a single API call."""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 16384
    thinking_enabled: bool = True
    thinking_budget: int = 10000
    temperature: float | None = None
    stop_sequences: list[str] | None = None
    timeout: int = 300  # seconds
    context_window: int = 0  # 0 means unknown / not resolved yet
    reasoning_effort: str | None = None  # call-level override for Responses-style APIs


@dataclass
class StreamChunk:
    """A single chunk from the streaming API response.

    Each chunk can carry text, tool use data, thinking, or metadata.
    """
    type: str  # text, thinking, tool lifecycle, message lifecycle, or error
    text: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input_json: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    error: str = ""


class APIAdapter(ABC):
    """Abstract interface for LLM API backends.

    All adapters translate between CrabCode's internal Anthropic-style
    message format and the provider's native format.
    """

    config: Any  # ApiConfig — set by concrete subclasses

    @abstractmethod
    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Send messages and stream back response chunks."""
        ...
        yield  # pragma: no cover

    @abstractmethod
    async def count_tokens(
        self,
        messages: list[Message],
        system: list[str],
    ) -> int:
        """Estimate token count for a message list."""
        ...

    async def resolve_context_window(self) -> int:
        """Resolve the effective context window size for the current model.

        Priority: config.context_window (user override)
                  -> API query (Anthropic Models API)
                  -> built-in lookup table
                  -> DEFAULT_CONTEXT_WINDOW
        """
        from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window

        if hasattr(self, "config") and getattr(self.config, "context_window", None):
            return self.config.context_window

        model = getattr(self.config, "model", None) if hasattr(self, "config") else None
        looked_up = lookup_context_window(model)
        if looked_up is not None:
            return looked_up

        return DEFAULT_CONTEXT_WINDOW
