"""Core session — the main interface between frontends and the engine."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.event import (
    ChoiceResponseEvent,
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
        self._choice_queue: asyncio.Queue[ChoiceResponseEvent] = asyncio.Queue()
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

        # Keep a /model switch that ran before the first initialize() (late init).
        chosen = self._current_model_name
        if chosen is None or chosen not in merged.models:
            chosen = merged.default_model
        self._current_model_name = chosen
        active_api_config = merged.get_api_config(self._current_model_name)
        self._api_adapter = create_adapter(active_api_config)

        if not self.tools:
            self.tools = get_default_tools()

        self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)

        # Write session meta to JSONL + SQLite
        active_cfg = merged.get_api_config(self._current_model_name)
        self._session_storage.write_meta(
            model=active_cfg.model or "",
            provider=active_cfg.provider or "",
        )

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
            agent_cfg = merged.agent
            self.tools.append(AgentTool(
                api_adapter=self._api_adapter,
                tools=sub_tools,
                prompt_profile=self._prompt_profile,
                max_turns=agent_cfg.max_turns,
                timeout=agent_cfg.timeout,
                max_output_chars=agent_cfg.max_output_chars,
                max_display_lines=merged.display.get_max_lines("Agent"),
            ))

        from crabcode_core.skills.loader import load_skills
        from crabcode_core.tools.skill import SkillTool

        self.skills = load_skills(self.cwd)
        if self.skills:
            self.tools.append(SkillTool(self.skills))

        await asyncio.gather(*(t.resolve_prompt() for t in self.tools))

        self._initialized = True

    # --- Context extraction helpers for skill auto-trigger ---

    @staticmethod
    def _extract_file_paths(text: str) -> list[str]:
        """Extract potential file paths from user message text.

        Looks for quoted paths, paths with extensions, and common path patterns.
        """
        import re

        paths: list[str] = []
        # Quoted paths: "src/foo.py" or 'src/foo.py'
        for m in re.finditer(r'["\']([^\s"\']+\.[\w]+)["\']', text):
            paths.append(m.group(1))
        # Unquoted paths with extensions: src/foo.py
        for m in re.finditer(r'(?<!["\w])([\w./\\-]+\.[\w]{1,10})(?!["\w])', text):
            candidate = m.group(1)
            if not candidate.startswith(("http://", "https://")):
                paths.append(candidate)
        return paths

    @staticmethod
    def _extract_bash_commands(text: str) -> list[str]:
        """Extract potential bash commands from user message text.

        Looks for backtick-wrapped commands and common command patterns.
        """
        import re

        commands: list[str] = []
        # Backtick-wrapped commands: `git commit -m "..."`
        for m in re.finditer(r'`([^`]+)`', text):
            commands.append(m.group(1))
        # Lines starting with common command prefixes
        for m in re.finditer(r'(?:^|\n)\s*(git|npm|yarn|pip|python|cargo|make|docker|kubectl)\s+(\S.*)', text):
            commands.append(f"{m.group(1)} {m.group(2)}".strip())
        return commands

    @staticmethod
    def _extract_import_lines(text: str) -> list[str]:
        """Extract import/require lines from user message text."""
        import re

        lines: list[str] = []
        # Python: import X / from X import Y
        for m in re.finditer(r'(?<!\w)(import\s+[\w.]+|from\s+[\w.]+\s+import\s+[\w.*]+)', text):
            lines.append(m.group(0).strip())
        # JS/TS: require('X') / import X from 'Y'
        for m in re.finditer(r"(?<!\w)require\s*\(['\"][^'\"]+['\"]\)", text):
            lines.append(m.group(0).strip())
        for m in re.finditer(r"(?<!\w)import\s+[\w{} ,]+\s+from\s+['\"][^'\"]+['\"]", text):
            lines.append(m.group(0).strip())
        return lines

    async def send_message(
        self,
        text: str,
        max_turns: int = 0,
    ) -> AsyncGenerator[CoreEvent, None]:
        """Send a user message and stream back events."""
        await self.initialize()
        self._abort_controller.clear()

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

        # --- Skill auto-trigger ---
        if self.skills:
            from crabcode_core.skills.matcher import auto_match

            file_paths = self._extract_file_paths(text)
            bash_commands = self._extract_bash_commands(text)
            import_lines = self._extract_import_lines(text)

            auto_skills = auto_match(
                self.skills,
                file_paths=file_paths,
                bash_commands=bash_commands,
                import_lines=import_lines,
            )

            if auto_skills:
                skill_parts = []
                for skill in auto_skills:
                    header = f"[Auto-triggered skill: {skill.name}]"
                    if skill.description:
                        header += f" {skill.description}"
                    skill_parts.append(f"{header}\n{skill.content}")
                skill_context = "\n\n---\n\n".join(skill_parts)

                context_msg = create_user_message(
                    content=(
                        "<system-reminder>\n"
                        "The following skills were automatically triggered based on "
                        "your current context. Follow their instructions when relevant "
                        "to the user's request.\n\n"
                        f"{skill_context}\n"
                        "</system-reminder>"
                    ),
                )
                self.messages.append(context_msg)

        if self._session_storage:
            self._session_storage.append_message(user_msg)
            # Update first_user_message in meta on the first real user message
            if not self._session_storage.meta.get("first_user_message"):
                active_api_cfg = self.settings.get_api_config(self._current_model_name)
                self._session_storage.write_meta(
                    model=active_api_cfg.model or "",
                    provider=active_api_cfg.provider or "",
                    first_user_message=text,
                )

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
            choice_queue=self._choice_queue,
            tool_event_queue=asyncio.Queue(),
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
                    # Record token usage and message count
                    total_tokens = event.usage.get("input_tokens", 0) + event.usage.get("output_tokens", 0)
                    if total_tokens > 0:
                        self._session_storage.record_tokens(total_tokens)
                    self._session_storage.record_message_count(len(self.messages))

            yield event

    async def respond_permission(self, response: PermissionResponseEvent) -> None:
        await self._permission_queue.put(response)

    async def respond_choice(self, response: ChoiceResponseEvent) -> None:
        await self._choice_queue.put(response)

    async def interrupt(self) -> None:
        self._abort_controller.set()

    def record_partial_assistant_output(self, text: str) -> None:
        """Append assistant text when a turn stops mid-stream so the next round keeps context."""
        if not text or not text.strip():
            return
        from crabcode_core.types.message import TextBlock, create_assistant_message

        assistant_msg = create_assistant_message(content=[TextBlock(text=text)])
        self.messages.append(assistant_msg)
        if self._session_storage:
            self._session_storage.append_message(assistant_msg)

    def new_session(self) -> str:
        """Start a fresh session, preserving tools and config. Returns the new session ID."""
        from crabcode_core.session.storage import SessionStorage, generate_session_id

        self.messages.clear()
        self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)
        # Write meta for the new session
        if self._initialized:
            active_api_cfg = self.settings.get_api_config(self._current_model_name)
            self._session_storage.write_meta(
                model=active_api_cfg.model or "",
                provider=active_api_cfg.provider or "",
            )
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

        # Sync meta to SQLite if it was read from JSONL but missing in DB
        if storage.meta and self._initialized:
            try:
                from crabcode_core.session.meta_db import SessionMetaStore
                store = SessionMetaStore()
                existing = store.get(session_id)
                if not existing:
                    meta = storage.meta
                    created_at = meta.get("created_at", "")
                    updated_at = meta.get("updated_at", "")
                    # Parse ISO timestamps to unix if needed
                    def _to_unix(ts: Any) -> int:
                        if isinstance(ts, (int, float)):
                            return int(ts)
                        if isinstance(ts, str) and ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                return int(dt.timestamp())
                            except Exception:
                                pass
                        return int(datetime.now(timezone.utc).timestamp())
                    sqlite_meta = {
                        "id": session_id,
                        "title": meta.get("title", ""),
                        "cwd": self.cwd,
                        "model": meta.get("model", ""),
                        "provider": meta.get("provider", ""),
                        "first_user_message": meta.get("first_user_message", ""),
                        "tokens_used": meta.get("tokens_used", 0),
                        "git_branch": meta.get("git_branch"),
                        "git_sha": meta.get("git_sha"),
                        "created_at": _to_unix(created_at),
                        "updated_at": _to_unix(updated_at),
                        "message_count": meta.get("message_count", len(raw_messages)),
                    }
                    store.upsert(sqlite_meta)
                store.close()
            except Exception:
                pass

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
