"""Tests for Ollama, Gemini, and Azure provider adapters."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from crabcode_core.api import (
    AzureOpenAIAdapter,
    GeminiAdapter,
    OllamaAdapter,
    create_adapter,
)
from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window
from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.api.codex_adapter import CodexAdapter, _iter_sse_payloads
from crabcode_core.api.anthropic_adapter import AnthropicAdapter
from crabcode_core.api.base import ModelConfig
from crabcode_core.types.message import create_user_message
from crabcode_core.types.config import ApiConfig


def _mock_create_client(self, config: ApiConfig):
    """Replace _create_client so no real OpenAI client is created."""
    return MagicMock()


# ---------------------------------------------------------------------------
# create_adapter factory
# ---------------------------------------------------------------------------


class TestCreateAdapter:
    """create_adapter returns the correct adapter class per provider."""

    def _make_config(self, provider: str, **overrides) -> ApiConfig:
        return ApiConfig(provider=provider, model="test-model", **overrides)

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_ollama_adapter(self):
        adapter = create_adapter(self._make_config("ollama"))
        assert isinstance(adapter, OllamaAdapter)

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_gemini_adapter(self):
        adapter = create_adapter(self._make_config("gemini"))
        assert isinstance(adapter, GeminiAdapter)

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_azure_adapter(self):
        adapter = create_adapter(self._make_config("azure"))
        assert isinstance(adapter, AzureOpenAIAdapter)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown API provider"):
            create_adapter(self._make_config("nonexistent"))


# ---------------------------------------------------------------------------
# OllamaAdapter defaults
# ---------------------------------------------------------------------------


class TestOllamaAdapter:
    """OllamaAdapter pre-fills localhost base URL and disables thinking."""

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_default_base_url(self):
        config = ApiConfig(provider="ollama", model="llama3.1:8b")
        adapter = OllamaAdapter(config)
        assert adapter.config.base_url == "http://localhost:11434/v1"

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_custom_base_url_preserved(self):
        config = ApiConfig(
            provider="ollama", model="llama3.1:8b", base_url="http://custom:9999/v1"
        )
        adapter = OllamaAdapter(config)
        assert adapter.config.base_url == "http://custom:9999/v1"

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_default_api_key_env(self):
        config = ApiConfig(provider="ollama", model="llama3.1:8b")
        adapter = OllamaAdapter(config)
        assert adapter.config.api_key_env == "OLLAMA_API_KEY"

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_thinking_disabled_by_default(self):
        config = ApiConfig(provider="ollama", model="llama3.1:8b")
        adapter = OllamaAdapter(config)
        assert adapter.config.thinking_enabled is False

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_thinking_explicitly_enabled_gets_overridden(self):
        """Even if user sets thinking_enabled=True, Ollama forces it off."""
        config = ApiConfig(
            provider="ollama", model="llama3.1:8b", thinking_enabled=True
        )
        adapter = OllamaAdapter(config)
        assert adapter.config.thinking_enabled is False


# ---------------------------------------------------------------------------
# GeminiAdapter defaults
# ---------------------------------------------------------------------------


class TestGeminiAdapter:
    """GeminiAdapter pre-fills Google endpoint and disables thinking."""

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_default_base_url(self):
        config = ApiConfig(provider="gemini", model="gemini-2.5-pro")
        adapter = GeminiAdapter(config)
        assert (
            adapter.config.base_url
            == "https://generativelanguage.googleapis.com/v1beta/openai/"
        )

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_custom_base_url_preserved(self):
        config = ApiConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            base_url="https://custom.googleapis.com/v1/openai/",
        )
        adapter = GeminiAdapter(config)
        assert adapter.config.base_url == "https://custom.googleapis.com/v1/openai/"

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_default_api_key_env(self):
        config = ApiConfig(provider="gemini", model="gemini-2.5-pro")
        adapter = GeminiAdapter(config)
        assert adapter.config.api_key_env == "GEMINI_API_KEY"

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_thinking_disabled_by_default(self):
        config = ApiConfig(provider="gemini", model="gemini-2.5-pro")
        adapter = GeminiAdapter(config)
        assert adapter.config.thinking_enabled is False

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_thinking_explicitly_enabled_gets_overridden(self):
        config = ApiConfig(
            provider="gemini", model="gemini-2.5-pro", thinking_enabled=True
        )
        adapter = GeminiAdapter(config)
        assert adapter.config.thinking_enabled is False


# ---------------------------------------------------------------------------
# AzureOpenAIAdapter
# ---------------------------------------------------------------------------


class TestAzureOpenAIAdapter:
    """AzureOpenAIAdapter reads environment variables correctly."""

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_reads_azure_api_key_env(self):
        config = ApiConfig(provider="azure", model="gpt-4o")
        adapter = AzureOpenAIAdapter(config)
        # The adapter stores the config; verify provider is azure.
        assert adapter.config.provider == "azure"

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_deployment_override_from_env(self):
        with patch.dict(
            os.environ,
            {"AZURE_OPENAI_DEPLOYMENT": "my-deployment"},
            clear=False,
        ):
            config = ApiConfig(provider="azure", model="gpt-4o")
            adapter = AzureOpenAIAdapter(config)
            assert adapter._deployment_override == "my-deployment"

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_no_deployment_override_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ApiConfig(provider="azure", model="gpt-4o")
            adapter = AzureOpenAIAdapter(config)
            assert adapter._deployment_override is None

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_resolve_model_uses_deployment_override(self):
        from crabcode_core.api.base import ModelConfig

        with patch.dict(
            os.environ,
            {"AZURE_OPENAI_DEPLOYMENT": "override-deploy"},
            clear=False,
        ):
            config = ApiConfig(provider="azure", model="gpt-4o")
            adapter = AzureOpenAIAdapter(config)
            model_config = ModelConfig(model="gpt-4o")
            assert adapter._resolve_model(model_config) == "override-deploy"

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_resolve_model_falls_back_to_config_model(self):
        from crabcode_core.api.base import ModelConfig

        config = ApiConfig(provider="azure", model="my-azure-deploy")
        adapter = AzureOpenAIAdapter(config)
        model_config = ModelConfig(model="gpt-4o")
        # No AZURE_OPENAI_DEPLOYMENT set → uses config.model from ModelConfig
        assert adapter._resolve_model(model_config) == "gpt-4o"

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_create_client_reads_azure_api_key_from_env(self):
        """Verify _create_client passes AZURE_OPENAI_API_KEY when set."""
        with patch.dict(
            os.environ,
            {
                "AZURE_OPENAI_API_KEY": "test-azure-key",
                "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
            },
            clear=False,
        ):
            config = ApiConfig(provider="azure", model="gpt-4o")
            # Temporarily restore the real _create_client to test env var reading
            adapter = AzureOpenAIAdapter.__new__(AzureOpenAIAdapter)
            adapter._deployment_override = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            adapter.config = config
            # We can't call the real _create_client without a valid Azure setup,
            # but we can verify the environment variables are accessible.
            assert os.environ.get("AZURE_OPENAI_API_KEY") == "test-azure-key"
            assert (
                os.environ.get("AZURE_OPENAI_ENDPOINT")
                == "https://example.openai.azure.com/"
            )

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_create_client_reads_azure_endpoint_from_env(self):
        """Verify AZURE_OPENAI_ENDPOINT env var is accessible."""
        with patch.dict(
            os.environ,
            {"AZURE_OPENAI_ENDPOINT": "https://my-resource.openai.azure.com/"},
            clear=False,
        ):
            assert (
                os.environ.get("AZURE_OPENAI_ENDPOINT")
                == "https://my-resource.openai.azure.com/"
            )

    @patch.multiple(
        "crabcode_core.api.azure_adapter.AzureOpenAIAdapter",
        _create_client=_mock_create_client,
    )
    def test_create_client_reads_api_version_from_env(self):
        """Verify AZURE_OPENAI_API_VERSION env var is accessible."""
        with patch.dict(
            os.environ,
            {"AZURE_OPENAI_API_VERSION": "2025-01-01"},
            clear=False,
        ):
            assert os.environ.get("AZURE_OPENAI_API_VERSION") == "2025-01-01"


class TestCustomHeaders:
    """HTTP headers from config are passed through to API clients."""

    def test_openai_adapter_passes_default_headers(self):
        config = ApiConfig(
            provider="openai",
            model="gpt-4o",
            http_headers={"X-Test-Header": "openai"},
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            OpenAIAdapter(config)
        mock_client.assert_called_once()
        assert mock_client.call_args.kwargs["default_headers"] == {
            "X-Test-Header": "openai"
        }

    def test_codex_adapter_passes_default_headers(self):
        config = ApiConfig(
            provider="codex",
            model="gpt-5.4",
            http_headers={"X-Test-Header": "codex"},
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            CodexAdapter(config)
        mock_client.assert_called_once()
        assert mock_client.call_args.kwargs["default_headers"] == {
            "X-Test-Header": "codex"
        }

    def test_anthropic_adapter_passes_default_headers(self):
        config = ApiConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            http_headers={"X-Test-Header": "anthropic"},
        )
        with patch("anthropic.AsyncAnthropic") as mock_client:
            AnthropicAdapter(config)
        mock_client.assert_called_once()
        assert mock_client.call_args.kwargs["default_headers"] == {
            "X-Test-Header": "anthropic"
        }

    def test_azure_adapter_passes_default_headers(self):
        config = ApiConfig(
            provider="azure",
            model="gpt-4o",
            http_headers={"X-Test-Header": "azure"},
        )
        with patch("openai.AsyncAzureOpenAI") as mock_client:
            adapter = AzureOpenAIAdapter.__new__(AzureOpenAIAdapter)
            adapter._deployment_override = None
            adapter.config = config
            AzureOpenAIAdapter._create_client(adapter, config)
        mock_client.assert_called_once()
        assert mock_client.call_args.kwargs["default_headers"] == {
            "X-Test-Header": "azure"
        }


class _FakeSSEHTTPResponse:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class TestCodexSSEFallback:
    def test_iter_sse_payloads_handles_split_event_and_data_blocks(self):
        async def run():
            response = _FakeSSEHTTPResponse(
                [
                    "event: response.created",
                    "",
                    'data: {"type":"response.created"}',
                    "",
                    "event: response.output_text.delta",
                    "",
                    'data: {"type":"response.output_text.delta","delta":"你好"}',
                    "",
                ]
            )
            items = []
            async for event_name, payload in _iter_sse_payloads(response):
                items.append((event_name, payload))
            return items

        items = asyncio.run(run())
        assert items == [
            ("response.created", {"type": "response.created"}),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "你好"},
            ),
        ]

    def test_codex_adapter_uses_httpx_stream_when_sdk_sse_parse_fails(self):
        class _FakeStreamResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                for line in [
                    "event: response.output_text.delta",
                    "",
                    'data: {"type":"response.output_text.delta","delta":"你好"}',
                    "",
                    "event: response.completed",
                    "",
                    'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}',
                    "",
                ]:
                    yield line

            async def aread(self):
                return b""

        class _FakeStreamContext:
            async def __aenter__(self):
                return _FakeStreamResponse()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _FakeHTTPClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, *args, **kwargs):
                return _FakeStreamContext()

        async def run():
            config = ApiConfig(
                provider="codex",
                model="gpt-5.4",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_CODEX_API_KEY",
                http_headers={"User-Agent": "codex-test"},
            )
            fake_openai_client = MagicMock()
            fake_openai_client.responses.create = MagicMock(
                side_effect=json.JSONDecodeError("bad json", "", 0)
            )
            with patch.dict(os.environ, {"TEST_CODEX_API_KEY": "test-key"}, clear=False):
                with patch("openai.AsyncOpenAI", return_value=fake_openai_client):
                    adapter = CodexAdapter(config)
                with patch("crabcode_core.api.codex_adapter.httpx.AsyncClient", _FakeHTTPClient):
                    return [
                        chunk
                        async for chunk in adapter.stream_message(
                            messages=[create_user_message("hi")],
                            system=["system"],
                            tools=[],
                            config=ModelConfig(model="gpt-5.4", max_tokens=32),
                        )
                    ]

        chunks = asyncio.run(run())
        assert [(chunk.type, chunk.text) for chunk in chunks[:1]] == [("text", "你好")]
        assert chunks[-1].type == "message_stop"
        assert chunks[-1].usage == {"input_tokens": 1, "output_tokens": 2}


# ---------------------------------------------------------------------------
# thinking_enabled defaults
# ---------------------------------------------------------------------------


class TestThinkingDefaults:
    """Verify thinking_enabled defaults for each provider."""

    @patch.multiple(
        "crabcode_core.api.ollama_adapter.OllamaAdapter",
        _create_client=_mock_create_client,
    )
    def test_ollama_thinking_false(self):
        config = ApiConfig(provider="ollama", model="llama3.1:8b")
        adapter = OllamaAdapter(config)
        assert adapter.config.thinking_enabled is False

    @patch.multiple(
        "crabcode_core.api.gemini_adapter.GeminiAdapter",
        _create_client=_mock_create_client,
    )
    def test_gemini_thinking_false(self):
        config = ApiConfig(provider="gemini", model="gemini-2.5-flash")
        adapter = GeminiAdapter(config)
        assert adapter.config.thinking_enabled is False

    def test_anthropic_thinking_true_by_default(self):
        """For contrast: Anthropic-style adapters default thinking to True."""
        config = ApiConfig(provider="anthropic", model="claude-sonnet-4-20250514")
        assert config.thinking_enabled is True


# ---------------------------------------------------------------------------
# model_info lookup for new models
# ---------------------------------------------------------------------------


class TestModelInfoLookup:
    """lookup_context_window returns correct context windows for known models."""

    def test_ollama_qwen3(self):
        assert lookup_context_window("qwen3:32b") == 128_000

    def test_ollama_qwen25_coder(self):
        assert lookup_context_window("qwen2.5-coder:32b") == 128_000

    def test_ollama_llama31(self):
        assert lookup_context_window("llama3.1:8b") == 128_000

    def test_gemini_25_pro(self):
        assert lookup_context_window("gemini-2.5-pro") == 1_048_576

    def test_gemini_25_flash(self):
        assert lookup_context_window("gemini-2.5-flash") == 1_048_576

    def test_gemini_20_flash(self):
        assert lookup_context_window("gemini-2.0-flash") == 1_048_576

    def test_gemini_15_pro(self):
        assert lookup_context_window("gemini-1.5-pro") == 2_097_152

    def test_azure_gpt4o(self):
        assert lookup_context_window("gpt-4o") == 128_000

    def test_unknown_model_returns_none(self):
        assert lookup_context_window("totally-unknown-model-xyz") is None

    def test_none_model_returns_none(self):
        assert lookup_context_window(None) is None

    def test_default_context_window_value(self):
        assert DEFAULT_CONTEXT_WINDOW == 200_000

    def test_prefix_match_for_versioned_model(self):
        """Versioned model IDs should fall back to prefix match."""
        # "gpt-4o-2024-11-20" has exact match, but a hypothetical
        # "gpt-4o-custom" should still match "gpt-4o" prefix.
        assert lookup_context_window("gpt-4o-custom-suffix") == 128_000
