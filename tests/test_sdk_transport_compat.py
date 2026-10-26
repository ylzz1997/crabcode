"""Exercise real provider SDKs without credentials or provider requests."""
import asyncio

import pytest

from crabcode_core.api.anthropic_adapter import AnthropicAdapter
from crabcode_core.api.azure_adapter import AzureOpenAIAdapter
from crabcode_core.api.codex_adapter import CodexAdapter
from crabcode_core.api.openai_adapter import OpenAIAdapter
from crabcode_core.types.config import ApiConfig


@pytest.mark.parametrize("adapter_type", [AnthropicAdapter, OpenAIAdapter, CodexAdapter, AzureOpenAIAdapter])
@pytest.mark.parametrize("mode", ["inherit", "direct", "proxy"])
def test_provider_client_initialization_and_reset(adapter_type, mode, monkeypatch):
    monkeypatch.setenv("CRABCODE_TEST_API_KEY", "local-test-no-provider-request")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    config = ApiConfig(
        api_key_env="CRABCODE_TEST_API_KEY", base_url="http://127.0.0.1:1",
        model="test-model", timeout=25, network_mode=mode,
        proxy_url="http://127.0.0.1:2" if mode == "proxy" else None,
    )

    async def check():
        adapter = adapter_type(config)
        original = adapter.client
        try:
            assert original.timeout.connect == 10
            assert original.timeout.read == 25
            assert original.max_retries == 0
            await adapter.reset_network_client()
            assert adapter.client is not original
            assert original.is_closed()
            assert adapter.client.timeout.connect == 10
            assert adapter.client.timeout.read == 25
            assert adapter.client.max_retries == 0
        finally:
            await adapter.client.close()
            if not original.is_closed():
                await original.close()

    asyncio.run(check())
