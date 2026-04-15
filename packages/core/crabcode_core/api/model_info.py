"""Known model context window sizes and resolution helpers."""

from __future__ import annotations

# Maps model ID (or prefix) to context window size in tokens.
# Used as fallback when the API doesn't provide model metadata.
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-5-20241022": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-sonnet-20240229": 200_000,
    "claude-3-haiku-20240307": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-2024-11-20": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "codex-mini-latest": 200_000,
    # DeepSeek
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    # GLM (Zhipu)
    "glm-5.1-fp8": 202_752,
    "glm-5.1": 202_752,
    "glm-4-plus": 128_000,
    "glm-4": 128_000,
}

DEFAULT_CONTEXT_WINDOW = 200_000


def lookup_context_window(model: str | None) -> int | None:
    """Look up the context window for a model ID.

    Tries exact match first, then prefix matching for versioned model IDs
    (e.g. "gpt-4o-2024-11-20" falls back to "gpt-4o").
    """
    if not model:
        return None

    if model in KNOWN_CONTEXT_WINDOWS:
        return KNOWN_CONTEXT_WINDOWS[model]

    for known_model, window in KNOWN_CONTEXT_WINDOWS.items():
        if model.startswith(known_model):
            return window

    return None
