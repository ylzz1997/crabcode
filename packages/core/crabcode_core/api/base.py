"""Abstract base for all API adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from crabcode_core.types.message import ContentBlock, Message


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


@dataclass
class StreamChunk:
    """A single chunk from the streaming API response.

    Each chunk can carry text, tool use data, thinking, or metadata.
    """
    type: str  # "text" | "tool_use_start" | "tool_use_delta" | "tool_use_end" | "thinking" | "message_start" | "message_delta" | "message_stop" | "error"
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
