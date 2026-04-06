"""Core session — the main interface between frontends and the engine."""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncGenerator

from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.event import (
    CompactEvent,
    CoreEvent,
    PermissionResponseEvent,
)
from crabcode_core.types.message import Message
from crabcode_core.types.tool import Tool, ToolEventCallback


class CoreSession:
    """Main entry point for frontends to interact with CrabCode.

    Holds conversation state, tools, and configuration.
    Frontends create a CoreSession and call send_message() to get
    an async stream of CoreEvents.
    """

    def __init__(
        self,
        cwd: str = ".",
        settings: CrabCodeSettings | None = None,
        tools: list[Tool] | None = None,
    ):
        self.cwd = os.path.abspath(cwd)
        self.settings = settings or CrabCodeSettings()
        self.messages: list[Message] = []
        self.tools: list[Tool] = tools or []
        self.session_id: str = ""
        self._permission_queue: asyncio.Queue[PermissionResponseEvent] = asyncio.Queue()
        self._abort_controller: asyncio.Event = asyncio.Event()

        self.skills: list = []
        self.on_tool_event: ToolEventCallback | None = None

        self._api_adapter: Any = None
        self._session_storage: Any = None
        self._permission_manager: Any = None
        self._mcp_manager: Any = None
        self._prompt_profile: Any = None
        self._initialized = False
        self._current_model_name: str | None = None
        self.compact_count: int = 0

    async def initialize(self) -> None:
        """Late initialization: set up API adapter, load tools, MCP, etc."""
        if self._initialized:
            return

        from crabcode_core.api import create_adapter
        from crabcode_core.config.manager import ConfigManager
        from crabcode_core.mcp.client import McpManager
        from crabcode_core.mcp.config import load_mcp_configs
        from crabcode_core.permissions.manager import PermissionManager
        from crabcode_core.session.storage import SessionStorage, generate_session_id
        from crabcode_core.tools import get_default_tools

        config_mgr = ConfigManager(cwd=self.cwd)
        file_settings = config_mgr.load()

        merged = self.settings

        for key, val in file_settings.env.items():
            os.environ.setdefault(key, val)

        if file_settings.api.provider and not self.settings.api.provider:
            merged.api.provider = file_settings.api.provider
        if file_settings.api.model and not self.settings.api.model:
            merged.api.model = file_settings.api.model
        if file_settings.api.base_url and not self.settings.api.base_url:
            merged.api.base_url = file_settings.api.base_url
        if file_settings.api.api_key_env and not self.settings.api.api_key_env:
            merged.api.api_key_env = file_settings.api.api_key_env
        if file_settings.api.format and not self.settings.api.format:
            merged.api.format = file_settings.api.format
        if file_settings.api.thinking_enabled is False and self.settings.api.thinking_enabled:
            merged.api.thinking_enabled = file_settings.api.thinking_enabled
        if file_settings.api.max_tokens != 16384 and self.settings.api.max_tokens == 16384:
            merged.api.max_tokens = file_settings.api.max_tokens

        if file_settings.models:
            for name, cfg in file_settings.models.items():
                merged.models.setdefault(name, cfg)
        if file_settings.default_model and not merged.default_model:
            merged.default_model = file_settings.default_model

        if file_settings.extra_tools and not self.settings.extra_tools:
            merged.extra_tools = file_settings.extra_tools
        if file_settings.tool_settings and not self.settings.tool_settings:
            merged.tool_settings = file_settings.tool_settings
        elif file_settings.tool_settings:
            for name, cfg in file_settings.tool_settings.items():
                merged.tool_settings.setdefault(name, {}).update(cfg)

        self._current_model_name: str | None = merged.default_model
        active_api_config = merged.get_api_config(self._current_model_name)
        self._api_adapter = create_adapter(active_api_config)

        if not self.tools:
            self.tools = get_default_tools()

        self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)

        self._permission_manager = PermissionManager(
            settings=merged.permissions,
        )

        mcp_configs = load_mcp_configs(self.cwd)
        all_mcp_configs = {**mcp_configs}
        for name, cfg in merged.mcp_servers.items():
            if name not in all_mcp_configs:
                all_mcp_configs[name] = cfg

        if all_mcp_configs:
            self._mcp_manager = McpManager()
            mcp_tools = await self._mcp_manager.connect(all_mcp_configs)
            existing_names = {t.name for t in self.tools}
            for mcp_tool in mcp_tools:
                if mcp_tool.name not in existing_names:
                    self.tools.append(mcp_tool)

        import importlib
        for tool_path in merged.extra_tools:
            try:
                module_path, class_name = tool_path.rsplit(".", 1)
                mod = importlib.import_module(module_path)
                tool_cls = getattr(mod, class_name)
                self.tools.append(tool_cls())
            except Exception:
                pass

        from crabcode_core.types.tool import ToolContext as _ToolContext

        async def _setup_tool(tool: Tool) -> None:
            ctx = _ToolContext(
                cwd=self.cwd,
                env=merged.env,
                on_event=self.on_tool_event,
                tool_config=merged.tool_settings.get(tool.name, {}),
            )
            await tool.setup(ctx)

        await asyncio.gather(*(_setup_tool(t) for t in self.tools))

        from crabcode_core.prompts.profile import PromptProfile
        from crabcode_core.tools.agent import AgentTool

        if self.settings.prompt_profile:
            self._prompt_profile = PromptProfile(**self.settings.prompt_profile)

        has_agent = any(isinstance(t, AgentTool) for t in self.tools)
        if not has_agent:
            sub_tools = list(self.tools)
            self.tools.append(AgentTool(
                api_adapter=self._api_adapter,
                tools=sub_tools,
                prompt_profile=self._prompt_profile,
            ))

        from crabcode_core.skills.loader import load_skills
        from crabcode_core.tools.skill import SkillTool

        self.skills = load_skills(self.cwd)
        if self.skills:
            self.tools.append(SkillTool(self.skills))

        await asyncio.gather(*(t.resolve_prompt() for t in self.tools))

        self._initialized = True

    async def send_message(
        self,
        text: str,
        max_turns: int = 0,
    ) -> AsyncGenerator[CoreEvent, None]:
        """Send a user message and stream back events."""
        await self.initialize()

        from crabcode_core.compact.compact import should_auto_compact, compact_conversation
        from crabcode_core.prompts.context import get_system_context, get_user_context
        from crabcode_core.prompts.profile import PromptProfile
        from crabcode_core.prompts.system import get_system_prompt
        from crabcode_core.query.loop import QueryParams, query_loop
        from crabcode_core.types.event import CompactEvent, TurnCompleteEvent
        from crabcode_core.types.message import create_user_message
        from crabcode_core.types.tool import ToolContext

        user_msg = create_user_message(content=text)
        self.messages.append(user_msg)

        if self._session_storage:
            self._session_storage.append_message(user_msg)

        compact_kwargs: dict[str, Any] = {}
        if self.settings.max_context_length is not None:
            compact_kwargs["threshold"] = self.settings.max_context_length

        if self.settings.auto_compact_enabled and should_auto_compact(self.messages, **compact_kwargs):
            compact_result = await compact_conversation(
                self.messages,
                api_adapter=self._api_adapter,
            )
            if compact_result:
                old_count = len(self.messages)
                self.messages = compact_result
                self.compact_count += 1
                yield CompactEvent(
                    summary="Conversation auto-compacted",
                    messages_before=old_count,
                    messages_after=len(self.messages),
                )

        tool_names = [t.name for t in self.tools]
        active_api_cfg = self.settings.get_api_config(self._current_model_name)
        model = active_api_cfg.model or "claude-sonnet-4-20250514"

        profile: PromptProfile | None = None
        if self.settings.prompt_profile:
            profile = PromptProfile(**self.settings.prompt_profile)

        system_prompt = get_system_prompt(
            enabled_tools=tool_names,
            model_id=model,
            cwd=self.cwd,
            language=self.settings.language,
            profile=profile,
        )
        system_context = get_system_context(self.cwd)
        user_context = get_user_context(self.cwd)

        tool_context = ToolContext(
            cwd=self.cwd,
            messages=self.messages,
            session_id=self.session_id,
            env=self.settings.env,
        )

        params = QueryParams(
            messages=list(self.messages),
            system_prompt=system_prompt,
            user_context=user_context,
            system_context=system_context,
            tools=self.tools,
            tool_context=tool_context,
            api_adapter=self._api_adapter,
            max_turns=max_turns or 0,
            permission_manager=self._permission_manager,
            permission_queue=self._permission_queue,
        )

        pre_loop_count = len(self.messages)

        async for event in query_loop(params):
            if isinstance(event, TurnCompleteEvent):
                self.messages = params.messages

                if self._session_storage:
                    for msg in self.messages[pre_loop_count:]:
                        self._session_storage.append_message(msg)

            yield event

    async def respond_permission(self, response: PermissionResponseEvent) -> None:
        await self._permission_queue.put(response)

    async def interrupt(self) -> None:
        self._abort_controller.set()

    def new_session(self) -> str:
        """Start a fresh session, preserving tools and config. Returns the new session ID."""
        from crabcode_core.session.storage import SessionStorage, generate_session_id

        self.messages.clear()
        self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)
        return self.session_id

    async def compact(self) -> None:
        """Manually trigger conversation compaction."""
        from crabcode_core.compact.compact import compact_conversation
        result = await compact_conversation(
            self.messages,
            api_adapter=self._api_adapter,
        )
        if result:
            self.messages = result
            self.compact_count += 1

    def list_models(self) -> dict[str, str]:
        """Return a dict of {name -> description} for all configured named models.

        The description is "<provider>/<model>" or just the model id if available.
        """
        result: dict[str, str] = {}
        for name, cfg in self.settings.models.items():
            parts = []
            if cfg.provider:
                parts.append(cfg.provider)
            if cfg.model:
                parts.append(cfg.model)
            result[name] = "/".join(parts) if parts else "(no model set)"
        return result

    def switch_model(self, name: str) -> bool:
        """Switch to a named model defined in settings.models.

        Returns True on success, False if the name is not found.
        Must be called after initialize().
        """
        if name not in self.settings.models:
            return False

        from crabcode_core.api import create_adapter
        from crabcode_core.tools.agent import AgentTool

        api_config = self.settings.models[name]
        self._api_adapter = create_adapter(api_config)
        self._current_model_name = name

        for tool in self.tools:
            if isinstance(tool, AgentTool):
                tool.api_adapter = self._api_adapter

        return True

    async def resume(self, session_id: str) -> bool:
        """Resume a previous session by loading its messages."""
        from crabcode_core.session.storage import SessionStorage
        from crabcode_core.types.message import (
            create_assistant_message,
            create_user_message,
            deserialize_content,
        )

        storage = SessionStorage(self.cwd, session_id)
        raw_messages = storage.load_messages()

        if not raw_messages:
            return False

        self.session_id = session_id
        self._session_storage = storage
        self.messages.clear()

        for raw in raw_messages:
            role = raw.get("type", "user")
            content = deserialize_content(raw.get("content", ""))
            msg_uuid = raw.get("uuid")
            parent_uuid = raw.get("parent_uuid")

            kwargs: dict[str, Any] = {}
            if msg_uuid:
                kwargs["uuid"] = msg_uuid
            if parent_uuid:
                kwargs["parent_uuid"] = parent_uuid

            if role == "user":
                self.messages.append(create_user_message(content=content, **kwargs))
            elif role == "assistant":
                self.messages.append(create_assistant_message(content=content, **kwargs))

        return True
