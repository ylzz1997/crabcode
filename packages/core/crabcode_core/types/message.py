"""Message types for the CrabCode conversation protocol.

Internal format follows Anthropic's message structure. API adapters
translate to/from provider-specific formats.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field
from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# --- Content blocks ---


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str


class SignatureBlock(BaseModel):
    type: Literal["signature"] = "signature"
    signature: str


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock, SignatureBlock]


# --- Messages ---


class Message(BaseModel):
    """Base message in a conversation."""

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_uuid: str | None = None
    role: MessageRole
    content: list[ContentBlock] | str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    is_compact_summary: bool = False

    @property
    def text_content(self) -> str:
        """Extract plain text from content blocks."""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        return "".join(parts)

    @property
    def tool_use_blocks(self) -> list[ToolUseBlock]:
        if isinstance(self.content, str):
            return []
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    @property
    def has_tool_use(self) -> bool:
        return len(self.tool_use_blocks) > 0


class UserMessage(Message):
    role: Literal[MessageRole.USER] = MessageRole.USER
    tool_use_result: str | None = None
    source_tool_assistant_uuid: str | None = None


class AssistantMessage(Message):
    role: Literal[MessageRole.ASSISTANT] = MessageRole.ASSISTANT
    api_error: str | None = None
    usage: dict[str, Any] | None = None
    request_id: str | None = None


class SystemMessage(Message):
    role: Literal[MessageRole.SYSTEM] = MessageRole.SYSTEM


def create_user_message(
    content: list[ContentBlock] | str,
    **kwargs: Any,
) -> UserMessage:
    return UserMessage(content=content, **kwargs)


def create_assistant_message(
    content: list[ContentBlock] | str,
    **kwargs: Any,
) -> AssistantMessage:
    return AssistantMessage(content=content, **kwargs)


def create_tool_result_message(
    tool_use_id: str,
    result: str,
    is_error: bool = False,
    source_tool_assistant_uuid: str | None = None,
) -> UserMessage:
    """Create a user message containing a tool result."""
    return UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=result,
                is_error=is_error,
            )
        ],
        parent_uuid=source_tool_assistant_uuid,
        tool_use_result=result,
        source_tool_assistant_uuid=source_tool_assistant_uuid,
    )


def deserialize_content(raw: Any) -> list[ContentBlock] | str:
    """Deserialize raw JSON content back into typed ContentBlock list."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return str(raw) if raw else ""

    _BLOCK_MAP: dict[str, type[BaseModel]] = {
        "text": TextBlock,
        "tool_use": ToolUseBlock,
        "tool_result": ToolResultBlock,
        "thinking": ThinkingBlock,
        "signature": SignatureBlock,
    }

    blocks: list[ContentBlock] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type", "")
        cls = _BLOCK_MAP.get(block_type)
        if cls:
            try:
                blocks.append(cls.model_validate(item))
            except Exception:
                logger.debug("Failed to deserialize message block type=%s", block_type, exc_info=True)
                if "text" in item:
                    blocks.append(TextBlock(text=item["text"]))
        elif "text" in item:
            blocks.append(TextBlock(text=item["text"]))

    return blocks if blocks else ""
