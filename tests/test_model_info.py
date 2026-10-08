"""Tests for model context-window resolution."""

from __future__ import annotations

import pytest

from crabcode_core.api.model_info import KNOWN_CONTEXT_WINDOWS, lookup_context_window
from crabcode_core.prompts.templates import CLAUDE_MODEL_IDS


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-fable-5-1", 1_000_000),
        ("claude-mythos-5-1", 1_000_000),
        ("claude-opus-5-5", 1_000_000),
        ("claude-haiku-5-5", 1_000_000),
        ("gpt-6-astra", 1_050_000),
        ("gpt-6.1-sol", 1_050_000),
        ("gpt-6-sol", 1_050_000),
        ("gpt-6-luna", 1_050_000),
        ("gpt-daybreak-blue-latest", 1_050_000),
        ("gpt-daybreak-red-latest", 400_000),
        ("grok-4.7", 500_000),
        ("grok-4.20", 1_000_000),
        ("grok-4.20-0309-reasoning", 1_000_000),
        ("grok-4.20-0309-non-reasoning", 1_000_000),
        ("grok-4.20-multi-agent-0309", 1_000_000),
        ("grok-4.6", 500_000),
        ("grok-4.5", 500_000),
        ("grok-4.3", 1_000_000),
        ("grok-build-latest", 500_000),
        ("grok-build-0.1", 256_000),
        ("grok-code-fast-1", 256_000),
        ("deepseek-flash", 1_000_000),
        ("deepseek-v4.1-flash", 1_000_000),
        ("mimo-v2.6-pro", 1_000_000),
        ("mimo-v2.6-pro-ultraspeed", 1_000_000),
        ("mimo-v2.6-flash", 1_000_000),
        ("mimo-v2.5", 1_000_000),
        ("mimo-v2.5-pro", 1_000_000),
        ("mimo-v2.5-asr", 8_000),
        ("mimo-v2.5-tts", 8_000),
        ("mimo-v2-flash", 256_000),
        ("mimo-v2-pro", 1_000_000),
        ("mimo-v2-omni", 256_000),
        ("mimo-7b-rl", 32_768),
        ("glm-5.3", 1_000_000),
        ("glm-5.3-flash", 1_000_000),
        ("qwen3.8-flash", 1_000_000),
        ("MiniMax-M3", 1_000_000),
        ("skyclaw-v1", 1_000_000),
        ("skyclaw-v1-lite", 1_000_000),
        ("muse-spark-1.3", 1_000_000),
        ("muse-spark-1.3-contributor", 1_000_000),
        ("Llama-4-Scout-17B-16E-Instruct", 10_000_000),
        ("Llama-4-Maverick-17B-128E-Instruct", 1_000_000),
        ("Llama-3.3-70B-Instruct", 128_000),
        ("Llama-3.2-3B-Instruct", 128_000),
        ("Llama-3.1-8B-Instruct", 128_000),
        ("Llama-3-8B-Instruct", 8_000),
        ("Llama-2-7b-chat-hf", 4_000),
        ("Llama-1-7B", 2_048),
        ("IFM/K2-Horizon-375B-A23B", 524_288),
        ("IFM/K2-Horizon-7B", 524_288),
        ("IFM/K2-Horizon-0.9B", 131_072),
        ("thinkingmachines/Inkling", 1_048_576),
        ("thinkingmachines/Inkling-Small", 1_048_576),
        ("inkling-small-free", 1_048_576),
        ("NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4", 1_000_000),
        ("NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4", 1_000_000),
        ("NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4", 1_000_000),
        ("Nemotron-Cascade-2-30B-A3B", 1_000_000),
        ("NVIDIA-Nemotron-Nano-12B-v2-VL-FP8", 128_000),
        ("Llama-3.1-Nemotron-Nano-4B-v1.1", 131_072),
        ("Nemotron-H-4B-Instruct-128K", 128_000),
        ("Nemotron-Mini-4B-Instruct", 4_096),
        ("Nemotron-4-340B-Instruct", 4_096),
        ("mistral-medium-3-5", 256_000),
        ("mistral-small-2603", 256_000),
        ("mistral-large-2512", 256_000),
        ("open-mixtral-8x22b", 64_000),
        ("open-mixtral-8x7b", 32_000),
        ("Mixtral-8x22B-Instruct-v0.1", 64_000),
        ("Mixtral-8x7B-Instruct-v0.1", 32_000),
        ("gemini-3.8-flash", 1_048_576),
        ("gemini-3.8-flash-tts", 1_048_576),
        ("gemini-3.8-flash-lite-tts", 1_048_576),
        ("gemini-nano-banana-2.1", 1_048_576),
        ("deep-research-preview-04-2026", 1_048_576),
        ("deep-research-max-preview-04-2026", 1_048_576),
        ("antigravity-preview-09-2026", 1_048_576),
        ("gemini-3.8-live", 131_072),
        ("gemini-3.8-live-extended-thinking", 131_072),
        ("gemini-3.7-flash", 1_048_576),
    ],
)
def test_exact_model_context_window(model: str, expected: int) -> None:
    """Return official limits for exact model IDs."""
    assert lookup_context_window(model) == expected


def test_versioned_model_uses_base_context_window() -> None:
    """Resolve a dated snapshot from its base model."""
    assert lookup_context_window("gpt-4o-2024-11-20") == 128_000


def test_colon_tag_uses_base_context_window() -> None:
    """Resolve a tagged model from its base model."""
    assert lookup_context_window("mistral:latest") == 32_000


def test_longest_model_name_wins_for_version_suffix() -> None:
    """Prefer a specific variant over a shorter base model."""
    assert lookup_context_window("gpt-5.6-cyber-2026-09-01") == 400_000
    assert lookup_context_window("grok-4.20-0309-reasoning") == 1_000_000
    assert lookup_context_window("grok-4.6-2026-08-12") == 500_000
    assert lookup_context_window("skyclaw-v1-lite-2026-05-19") == 1_000_000


@pytest.mark.parametrize("model", [None, "", "unknown-model", "gpt-4oextra"])
def test_unknown_or_similar_model_does_not_match(model: str | None) -> None:
    """Reject empty, unknown, and unbounded-prefix model names."""
    assert lookup_context_window(model) is None


def test_builtin_claude_presets_have_context_windows() -> None:
    """Keep built-in Claude presets aligned with the context map."""
    assert all(
        lookup_context_window(model) is not None for model in CLAUDE_MODEL_IDS.values()
    )


def test_context_windows_are_positive_integers() -> None:
    """Require usable token limits for every known model."""
    assert all(
        isinstance(context_window, int) and context_window > 0
        for context_window in KNOWN_CONTEXT_WINDOWS.values()
    )
