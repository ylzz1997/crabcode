"""Tests for the ACP (Agent Client Protocol) layer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crabcode_gateway.acp.agent import CrabCodeACPAgent, _build_config_options
from crabcode_gateway.acp.types import ACPConfig, ModelSelection, to_tool_kind, to_locations


# ── to_tool_kind ───────────────────────────────────────────────


class TestToToolKind:
    def test_bash(self):
        assert to_tool_kind("bash") == "execute"

    def test_edit(self):
        assert to_tool_kind("edit") == "edit"

    def test_file_edit(self):
        assert to_tool_kind("file_edit") == "edit"

    def test_file_write(self):
        assert to_tool_kind("file_write") == "edit"

    def test_write(self):
        assert to_tool_kind("write") == "edit"

    def test_grep(self):
        assert to_tool_kind("grep") == "search"

    def test_glob(self):
        assert to_tool_kind("glob") == "search"

    def test_read(self):
        assert to_tool_kind("read") == "read"

    def test_file_read(self):
        assert to_tool_kind("file_read") == "read"

    def test_web_search(self):
        assert to_tool_kind("web_search") == "fetch"

    def test_delete(self):
        assert to_tool_kind("delete") == "delete"

    def test_switch_mode(self):
        assert to_tool_kind("switch_mode") == "switch_mode"

    def test_unknown(self):
        assert to_tool_kind("mystery_tool") == "other"

    def test_case_insensitive(self):
        assert to_tool_kind("Bash") == "execute"
        assert to_tool_kind("EDIT") == "edit"


# ── to_locations ────────────────────────────────────────────────


class TestToLocations:
    def test_edit_with_filePath(self):
        assert to_locations("edit", {"filePath": "/tmp/a.py"}) == [{"path": "/tmp/a.py"}]

    def test_edit_with_file_path(self):
        assert to_locations("file_edit", {"file_path": "/tmp/b.py"}) == [{"path": "/tmp/b.py"}]

    def test_grep_with_path(self):
        assert to_locations("grep", {"path": "/src"}) == [{"path": "/src"}]

    def test_bash_no_location(self):
        assert to_locations("bash", {}) == []

    def test_edit_no_path(self):
        assert to_locations("edit", {}) == []


# ── CrabCodeACPAgent.initialize ────────────────────────────────


class TestInitialize:
    def test_initialize_returns_valid_response(self):
        config = ACPConfig(base_url="http://127.0.0.1:4096")
        agent = CrabCodeACPAgent(config)

        resp = asyncio.run(agent.initialize(protocol_version=1))

        assert resp.protocol_version == 1
        assert resp.agent_info is not None
        assert resp.agent_info.name == "CrabCode"
        assert resp.agent_info.version == "0.1.0"
        assert resp.agent_capabilities is not None
        assert resp.agent_capabilities.load_session is True
        assert resp.agent_capabilities.mcp_capabilities is not None
        assert resp.agent_capabilities.mcp_capabilities.http is True
        assert resp.agent_capabilities.prompt_capabilities is not None
        assert resp.agent_capabilities.prompt_capabilities.image is True
        assert resp.agent_capabilities.session_capabilities is not None
        assert len(resp.auth_methods) > 0


# ── CrabCodeACPAgent.new_session ───────────────────────────────


class TestNewSession:
    def test_new_session_creates_session(self):
        async def _test():
            config = ACPConfig(base_url="http://127.0.0.1:4096")
            agent = CrabCodeACPAgent(config)

            from crabcode_gateway.acp.types import ACPSessionState
            from acp.schema import SessionModelState, ModelInfo, SessionModeState, SessionMode

            with patch.object(agent._session_mgr, "create", new_callable=AsyncMock) as mock_create:
                mock_create.return_value = ACPSessionState(
                    id="test-123", cwd="/tmp", created_at=0.0,
                )

                with patch.object(agent, "_build_models_state", new_callable=AsyncMock) as mock_models:
                    mock_models.return_value = SessionModelState(
                        current_model_id="anthropic/claude-sonnet-4",
                        available_models=[ModelInfo(model_id="anthropic/claude-sonnet-4", name="Claude Sonnet")],
                    )

                    with patch.object(agent, "_build_modes_state", new_callable=AsyncMock) as mock_modes:
                        mock_modes.return_value = SessionModeState(
                            current_mode_id="agent",
                            available_modes=[SessionMode(id="agent", name="Agent")],
                        )

                        resp = await agent.new_session(cwd="/tmp")

            assert resp.session_id == "test-123"
            assert resp.models is not None
            assert resp.models.current_model_id == "anthropic/claude-sonnet-4"

        asyncio.run(_test())


# ── CrabCodeACPAgent.prompt ────────────────────────────────────


class TestPrompt:
    def test_prompt_sends_text(self):
        async def _test():
            config = ACPConfig(base_url="http://127.0.0.1:4096")
            agent = CrabCodeACPAgent(config)

            from crabcode_gateway.acp.types import ACPSessionState
            agent._session_mgr._sessions["sess-1"] = ACPSessionState(
                id="sess-1", cwd="/tmp", created_at=0.0,
            )

            mock_http_resp = MagicMock()
            mock_http_resp.raise_for_status = MagicMock()
            mock_http_resp.json.return_value = {"status": "started", "session_id": "sess-1"}

            with patch.object(agent._session_mgr._client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_http_resp

                from acp.schema import TextContentBlock

                resp = await agent.prompt(
                    prompt=[TextContentBlock(type="text", text="hello")],
                    session_id="sess-1",
                )

            assert resp.stop_reason == "end_turn"
            mock_post.assert_called_once()

        asyncio.run(_test())


# ── CrabCodeACPAgent.cancel ────────────────────────────────────


class TestCancel:
    def test_cancel_sends_interrupt(self):
        async def _test():
            config = ACPConfig(base_url="http://127.0.0.1:4096")
            agent = CrabCodeACPAgent(config)

            from crabcode_gateway.acp.types import ACPSessionState
            agent._session_mgr._sessions["sess-1"] = ACPSessionState(
                id="sess-1", cwd="/tmp", created_at=0.0,
            )

            with patch.object(agent._session_mgr._client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = MagicMock()
                await agent.cancel(session_id="sess-1")

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/session/interrupt" in call_args[0][0]

        asyncio.run(_test())


# ── _build_config_options ──────────────────────────────────────


class TestBuildConfigOptions:
    def test_builds_model_and_mode_options(self):
        from acp.schema import SessionModelState, ModelInfo, SessionModeState, SessionMode

        models = SessionModelState(
            current_model_id="anthropic/claude",
            available_models=[ModelInfo(model_id="anthropic/claude", name="Claude")],
        )
        modes = SessionModeState(
            current_mode_id="agent",
            available_modes=[SessionMode(id="agent", name="Agent")],
        )

        options = _build_config_options(models, modes)
        assert len(options) == 2
        assert options[0].id == "model"
        assert options[0].type == "select"
        assert options[1].id == "mode"
        assert options[1].type == "select"

    def test_builds_with_none(self):
        options = _build_config_options(None, None)
        assert options == []


# ── Session Manager ────────────────────────────────────────────


class TestACPSessionManager:
    def test_get_and_set_model(self):
        from crabcode_gateway.acp.session import ACPSessionManager
        from crabcode_gateway.acp.types import ACPSessionState

        mgr = ACPSessionManager(ACPConfig(base_url="http://127.0.0.1:4096"))
        mgr._sessions["s1"] = ACPSessionState(id="s1", cwd="/tmp", created_at=0.0)

        mgr.set_model("s1", ModelSelection(provider_id="anthropic", model_id="claude"))
        assert mgr.get_model("s1") == ModelSelection(provider_id="anthropic", model_id="claude")

    def test_get_and_set_mode(self):
        from crabcode_gateway.acp.session import ACPSessionManager
        from crabcode_gateway.acp.types import ACPSessionState

        mgr = ACPSessionManager(ACPConfig(base_url="http://127.0.0.1:4096"))
        mgr._sessions["s1"] = ACPSessionState(id="s1", cwd="/tmp", created_at=0.0)

        mgr.set_mode("s1", "plan")
        assert mgr.get_mode("s1") == "plan"

    def test_try_get_returns_none_for_unknown(self):
        from crabcode_gateway.acp.session import ACPSessionManager

        mgr = ACPSessionManager(ACPConfig(base_url="http://127.0.0.1:4096"))
        assert mgr.try_get("nonexistent") is None

    def test_get_raises_for_unknown(self):
        from crabcode_gateway.acp.session import ACPSessionManager
        from acp.exceptions import RequestError

        mgr = ACPSessionManager(ACPConfig(base_url="http://127.0.0.1:4096"))
        with pytest.raises(RequestError):
            mgr.get("nonexistent")
