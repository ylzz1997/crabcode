"""Known model context window sizes and resolution helpers."""

from __future__ import annotations

# Maps model ID (or prefix) to context window size in tokens.
# Used as fallback when the API doesn't provide model metadata.
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-mythos-preview": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-5-20241022": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-sonnet-20240229": 200_000,
    "claude-3-haiku-20240307": 200_000,
    # OpenAI
    "gpt-5.6-cyber": 400_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
    "gpt-5.6": 1_050_000,
    "gpt-5.5-pro": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.4-pro": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.3-chat-latest": 128_000,
    "gpt-5.3-codex": 400_000,
    "gpt-5.2-chat-latest": 128_000,
    "gpt-5.2-codex": 400_000,
    "gpt-5.2-pro": 400_000,
    "gpt-5.2": 400_000,
    "gpt-5.1-chat-latest": 128_000,
    "gpt-5.1-codex-mini": 400_000,
    "gpt-5.1-codex-max": 400_000,
    "gpt-5.1-codex": 400_000,
    "gpt-5.1": 400_000,
    "gpt-5-chat-latest": 128_000,
    "gpt-5-codex": 400_000,
    "gpt-5-mini": 400_000,
    "gpt-5-nano": 400_000,
    "gpt-5-pro": 400_000,
    "gpt-5": 400_000,
    "gpt-4.5-preview": 128_000,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "gpt-4.1": 1_047_576,
    "gpt-4o-mini-realtime-preview": 16_000,
    "gpt-4o-mini-transcribe": 16_000,
    "gpt-4o-realtime-preview": 32_000,
    "gpt-4o-transcribe-diarize": 16_000,
    "gpt-4o-transcribe": 16_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1-preview": 128_000,
    "o1-mini": 128_000,
    "o1-pro": 200_000,
    "o1": 200_000,
    "o3-deep-research": 200_000,
    "o3-mini": 200_000,
    "o3-pro": 200_000,
    "o3": 200_000,
    "o4-mini-deep-research": 200_000,
    "o4-mini": 200_000,
    "codex-mini-latest": 200_000,
    # DeepSeek
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    # GLM (Zhipu)
    "glm-5.2": 1_000_000,
    "glm-5.1-fp8": 202_752,
    "glm-5.1": 202_752,
    "glm-4-plus": 128_000,
    "glm-4": 128_000,
    # Other OpenAI-compatible models
    "qwen3.8-max": 1_000_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.7-flash": 1_000_000,
    "qwen3.6-plus": 1_000_000,
    "qwen3.6-flash": 1_000_000,
    "qwen3.5-plus": 1_000_000,
    "qwen3.5-flash": 1_000_000,
    "kimi-k3": 1_000_000,
    "kimi-k2.7-code": 262_144,
    "kimi-k2.6": 262_144,
    "kimi-k2.5": 262_144,
    "moonshot-v1-128k": 128_000,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-8k": 8_000,
    "minimax-m2.7": 204_800,
    # Ollama
    "qwen3:32b": 128_000,
    "qwen2.5-coder:32b": 128_000,
    "deepseek-coder-v2": 128_000,
    "llama3.1:8b": 128_000,
    "codellama": 16_000,
    "mistral": 32_000,
    "mixtral": 32_000,
    # Gemini
    "gemini-3.6-flash": 1_048_576,
    "gemini-3.5-flash-lite": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.1-pro": 1_048_576,
    "gemini-3.1-flash-lite": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash-lite": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
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

    for known_model in sorted(KNOWN_CONTEXT_WINDOWS, key=len, reverse=True):
        if model.startswith((f"{known_model}-", f"{known_model}:")):
            return KNOWN_CONTEXT_WINDOWS[known_model]

    return None
