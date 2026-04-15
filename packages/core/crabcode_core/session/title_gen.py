"""Async session title generation using an LLM."""

from __future__ import annotations

from typing import Any

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)

_TITLE_PROMPT = (
    "Generate a concise title (5-8 words, no quotes) that captures the main topic "
    "of the following conversation start. Reply with ONLY the title, nothing else."
)


async def generate_title(
    first_user_message: str,
    first_assistant_text: str,
    api_adapter: Any,
) -> str | None:
    """Generate a short title for a conversation using the LLM.

    Returns a title string, or None on failure.
    """
    if not first_user_message:
        return None

    content = f"User: {first_user_message[:500]}"
    if first_assistant_text:
        content += f"\n\nAssistant: {first_assistant_text[:500]}"

    try:
        from crabcode_core.types.message import create_user_message

        messages = [create_user_message(content=content)]
        response = await api_adapter.chat(
            messages=messages,
            system_prompt=_TITLE_PROMPT,
            tools=[],
            max_tokens=50,
        )
        title = ""
        for block in response.content:
            if hasattr(block, "text"):
                title += block.text
        title = title.strip().strip('"').strip("'").strip()
        if title and len(title) <= 200:
            return title
    except Exception:
        logger.debug("Failed to generate session title", exc_info=True)
    return None
