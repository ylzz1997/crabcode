"""Prompt profile compatibility, cache wire format and token-size guardrails."""

import asyncio
from types import SimpleNamespace

import pytest

from crabcode_core.api.anthropic_adapter import AnthropicAdapter, BedrockAdapter
from crabcode_core.api.base import ModelConfig
from crabcode_core.prompts.blocks import SystemPrompt
from crabcode_core.prompts.profile import PromptProfile
from crabcode_core.prompts.system import get_system_prompt
from crabcode_core.query.loop import _append_system_context
from crabcode_core.types.config import ApiConfig


def test_profile_overrides_disable_and_static_boundary_survive_context():
    profile = PromptProfile(intro="custom identity", doing_tasks="", extra_sections=["extra"],
                            session_guidance="custom runtime")
    prompt = get_system_prompt([], "gpt-test", profile=profile, is_git=True)
    assert prompt[0] == "custom identity"
    assert not any("# Doing tasks" in s for s in prompt)
    assert "extra" in prompt[:prompt.static_count]
    assert "custom runtime" in prompt[prompt.static_count:]
    extended = _append_system_context(prompt, {"git": "dirty"})
    assert extended.static_count == prompt.static_count
    assert extended[-1] == "git: dirty"
    text = "\n".join(extended)
    assert "Is a git repository: Yes" in text
    for invalid in ("__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__", "AskUserQuestion", "TodoWrite", "most recent Claude", "Fast mode"):
        assert invalid not in text


@pytest.mark.parametrize("mode,base_url,adapter_type,expected", [
    ("auto", None, AnthropicAdapter, True),
    ("auto", "https://gateway.example", AnthropicAdapter, False),
    ("enabled", "https://gateway.example", AnthropicAdapter, True),
    ("disabled", None, AnthropicAdapter, False),
    ("auto", None, BedrockAdapter, False),
])
def test_anthropic_explicit_boundary_only_on_supported_or_opted_in_endpoint(mode, base_url, adapter_type, expected):
    adapter = object.__new__(adapter_type)
    adapter.config = ApiConfig(model="claude-test", prompt_caching=mode, base_url=base_url)
    adapter.client = SimpleNamespace(base_url="https://api.anthropic.com")
    system = SystemPrompt(["first", "last static", "dynamic"], static_count=2)
    payload = adapter._request_params([], system, [], ModelConfig(model="claude-test"))
    blocks = payload["system"]
    assert ("cache_control" in blocks[1]) == expected
    assert "cache_control" not in blocks[0] and "cache_control" not in blocks[2]
    assert "cache_control" not in adapter._request_params([], ["plain"], [], ModelConfig(model="claude-test"))["system"][0]
    assert system == ["first", "last static", "dynamic"]


def test_plan_language_and_ultra_are_preserved():
    prompt = get_system_prompt(["Agent", "Skill"], "test", agent_mode="plan", language="Chinese", ultra_mode=True)
    text = "\n".join(prompt)
    assert "MUST NOT make any edits" in text
    assert "Always respond in Chinese" in text
    assert "ULTRA MODE" in text
    assert "# Skills" in text
    assert "# Doing tasks" not in text


def test_user_questions_require_web_search_when_available():
    text = "\n".join(get_system_prompt(["WebSearch"], "test"))
    assert "gather enough relevant sources before answering" in text
    assert "Do not answer from memory or speculation" in text
    assert "fall back to Browser" in text
    assert "If neither WebSearch nor Browser can support an answer" in text
    assert "Skip web search and Browser only when the user explicitly says not to search" in text


def test_default_prompt_requires_real_tool_search_before_deferred_tools():
    text = "\n".join(get_system_prompt(["ToolSearch"], "test"))
    assert "directory lists only tools whose schemas are not supplied yet" in text
    assert "first call ToolSearch with its exact listed name" in text
    assert "group is a category, not a name prefix" in text
    assert "Tools with supplied schemas are callable directly and need no further search" in text
    assert "never print pseudo-calls such as `<tool_call>`" in text
    assert "use the newly supplied schema in the next response" in text


def test_default_prompt_budget_guardrail():
    from scripts.prompt_budget import measure
    result = asyncio.run(measure())
    assert result["discovery"]["total_tokens"] < 6500
    assert result["discovery"]["tool_tokens"] < 4000
    assert result["discovery"]["directory_tokens"] < 500
    assert result["discovery"]["loaded_tools"] == 12
    assert result["discovery"]["total_tokens"] < result["eager"]["total_tokens"] * 0.65
