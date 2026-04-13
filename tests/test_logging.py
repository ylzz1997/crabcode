from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from crabcode_core.api.base import APIAdapter, ModelConfig, StreamChunk
from crabcode_core.events import CoreSession
from crabcode_core.logging_utils import configure_logging, get_log_path
from crabcode_core.mcp.client import McpManager
from crabcode_core.types.config import ApiConfig, CrabCodeSettings, LoggingSettings, McpServerConfig
from crabcode_core.types.message import Message


@contextlib.contextmanager
def _patched_storage_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch("crabcode_core.session.storage.Path.home", return_value=home), patch(
            "crabcode_core.session.meta_db.Path.home", return_value=home
        ):
            yield


class QuietAdapter(APIAdapter):
    def __init__(self, config: ApiConfig):
        self.config = config

    async def stream_message(
        self,
        messages: list[Message],
        system: list[str],
        tools: list[dict[str, object]],
        config: ModelConfig,
    ):
        yield StreamChunk(type="message_start", usage={"input_tokens": 1})
        yield StreamChunk(type="text", text="done")
        yield StreamChunk(type="message_stop", usage={"output_tokens": 1})

    async def count_tokens(self, messages: list[Message], system: list[str]) -> int:
        return 1


def test_extra_tool_load_failure_is_logged():
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp, _patched_storage_home():
            settings = CrabCodeSettings(
                api=ApiConfig(provider="openai", model="fake"),
                extra_tools=["missing.module.Tool"],
                logging=LoggingSettings(level="WARNING"),
            )
            session = CoreSession(cwd=tmp, settings=settings, tools=[])
            adapter = QuietAdapter(settings.api)
            with patch("crabcode_core.api.create_adapter", return_value=adapter), patch(
                "crabcode_core.api.registry.create_adapter", return_value=adapter
            ):
                await session.initialize()
            log_path = get_log_path(tmp, settings.logging)
            assert log_path.exists()
            content = log_path.read_text(encoding="utf-8")
            assert "Failed to load extra tool" in content
            assert "missing.module.Tool" in content

    asyncio.run(_run())


def test_mcp_connect_failure_is_logged():
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logging_settings = LoggingSettings(level="WARNING")
            configure_logging(tmp, logging_settings)
            manager = McpManager()

            async def _boom(name: str, config: McpServerConfig):
                raise RuntimeError(f"boom:{name}")

            manager._connect_server = _boom  # type: ignore[method-assign]
            await manager.connect({"demo": McpServerConfig(command=["demo"])})

            log_path = get_log_path(tmp, logging_settings)
            assert log_path.exists()
            content = log_path.read_text(encoding="utf-8")
            assert "Failed to connect MCP server: demo" in content
            assert "boom:demo" in content

    asyncio.run(_run())
