"""Core session — the main interface between frontends and the engine."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable
from xml.sax.saxutils import escape

from crabcode_core.agent_manager import AgentCompletion, AgentManager, AgentSnapshot
from crabcode_core.logging_utils import configure_logging, get_logger
from crabcode_core.lsp.manager import LSPManager
from crabcode_core.types.config import CrabCodeSettings
from crabcode_core.types.event import (
    ChoiceResponseEvent,
    CompactEvent,
    CoreEvent,
    ErrorEvent,
    ModeChangeEvent,
    PermissionResponseEvent,
    PlanReadyEvent,
    TurnCompleteEvent,
)
from crabcode_core.types.message import Message, find_assistant_reply, message_from_entry
from crabcode_core.types.tool import Tool, ToolEventCallback

logger = get_logger(__name__)


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

        self.last_context_used_tokens: int = 0
        self.last_context_window_tokens: int = 0

        self._api_adapter: Any = None
        self._session_storage: Any = None
        self._permission_manager: Any = None
        self._mcp_manager: Any = None
        self._prompt_profile: Any = None
        self._initialized = False
        self._current_model_name: str | None = None
        self.compact_count: int = 0
        self._agent_event_queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
        self._agent_manager: AgentManager | None = None
        self._hook_manager: Any = None
        self._lsp_manager: LSPManager | None = None
        self._closed = False
        self._agent_mode: str = "agent"  # "agent" | "plan"
        self._foreground_turn_active = False
        self._saved_permission_mode: Any = None
        self._current_plan: Any = None  # ExecutionPlan | None
        self._title_generation_task: asyncio.Task[None] | None = None
        self._team_manager: Any = None  # TeamManager
        self._turn_lock = asyncio.Lock()
        self._managed_callback_lock = asyncio.Lock()
        self._lifecycle_generation = 0
        self._agent_completion_queue: asyncio.Queue[tuple[int, AgentCompletion]] = asyncio.Queue()
        self._agent_completion_task: asyncio.Task[None] | None = None
        self._monitor_notification_queue: asyncio.Queue[tuple[int, str, str]] = (
            asyncio.Queue(maxsize=1000)
        )
        self._monitor_notification_task: asyncio.Task[None] | None = None
        self._monitor_manager: Any = None
        self._background_event_queue: asyncio.Queue[CoreEvent] = asyncio.Queue()
        self._background_event_sink: Callable[[CoreEvent], Awaitable[None]] | None = None
        self._pending_manual_compact: str | None = None
        self._persisted_compact_summaries: set[str] = set()
        # Extension UI override: "ask" | "run_everything" | "ai_review" | None (follow file init only)
        self._client_permission_mode_override: str | None = None

    async def initialize(self) -> None:
        """Late initialization: set up API adapter, load tools, MCP, etc."""
        if self._initialized:
            return
        if self._closed:
            raise RuntimeError("CoreSession is closed")

        from crabcode_core.api import create_adapter
        from crabcode_core.config.manager import ConfigManager
        from crabcode_core.mcp.client import McpManager
        from crabcode_core.mcp.config import load_mcp_configs
        from crabcode_core.permissions.ai_reviewer import AiPermissionReviewer
        from crabcode_core.permissions.manager import PermissionManager
        from crabcode_core.session.storage import (
            get_agent_transcript_path,
        )
        from crabcode_core.tools import get_default_tools

        configure_logging(self.cwd, self.settings.logging)
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
        if file_settings.api.codex_auth_path and not self.settings.api.codex_auth_path:
            merged.api.codex_auth_path = file_settings.api.codex_auth_path
        if file_settings.api.http_headers and not self.settings.api.http_headers:
            merged.api.http_headers = dict(file_settings.api.http_headers)
        if file_settings.api.format and not self.settings.api.format:
            merged.api.format = file_settings.api.format
        if (
            file_settings.api.anthropic_stream_transport != "auto"
            and self.settings.api.anthropic_stream_transport == "auto"
        ):
            merged.api.anthropic_stream_transport = file_settings.api.anthropic_stream_transport
        if file_settings.api.thinking_enabled is False and self.settings.api.thinking_enabled:
            merged.api.thinking_enabled = file_settings.api.thinking_enabled
        if file_settings.api.pass_reasoning_content and not self.settings.api.pass_reasoning_content:
            merged.api.pass_reasoning_content = file_settings.api.pass_reasoning_content
        if file_settings.api.max_tokens != 16384 and self.settings.api.max_tokens == 16384:
            merged.api.max_tokens = file_settings.api.max_tokens
        if (
            "max_retries" in file_settings.api.model_fields_set
            and "max_retries" not in self.settings.api.model_fields_set
        ):
            merged.api.max_retries = file_settings.api.max_retries
        if file_settings.api.extra_body and not self.settings.api.extra_body:
            merged.api.extra_body = dict(file_settings.api.extra_body)
        if file_settings.api.context_window and not self.settings.api.context_window:
            merged.api.context_window = file_settings.api.context_window
        if file_settings.api.azure_endpoint and not self.settings.api.azure_endpoint:
            merged.api.azure_endpoint = file_settings.api.azure_endpoint
        if file_settings.api.azure_api_version and not self.settings.api.azure_api_version:
            merged.api.azure_api_version = file_settings.api.azure_api_version
        if file_settings.api.azure_deployment and not self.settings.api.azure_deployment:
            merged.api.azure_deployment = file_settings.api.azure_deployment

        if file_settings.models:
            for name, cfg in file_settings.models.items():
                merged.models.setdefault(name, cfg)
        if file_settings.default_model and not merged.default_model:
            merged.default_model = file_settings.default_model

        if file_settings.permissions.allow and not self.settings.permissions.allow:
            merged.permissions.allow = file_settings.permissions.allow
        if file_settings.permissions.deny and not self.settings.permissions.deny:
            merged.permissions.deny = file_settings.permissions.deny
        if file_settings.permissions.ask and not self.settings.permissions.ask:
            merged.permissions.ask = file_settings.permissions.ask
        if file_settings.permissions.default_mode and not self.settings.permissions.default_mode:
            merged.permissions.default_mode = file_settings.permissions.default_mode
        if file_settings.permissions.additional_directories and not self.settings.permissions.additional_directories:
            merged.permissions.additional_directories = file_settings.permissions.additional_directories
        if file_settings.permissions.run_everything and not self.settings.permissions.run_everything:
            merged.permissions.run_everything = file_settings.permissions.run_everything
        if file_settings.permissions.ai_review != self.settings.permissions.ai_review:
            merged.permissions.ai_review = file_settings.permissions.ai_review

        if file_settings.extra_tools and not self.settings.extra_tools:
            merged.extra_tools = file_settings.extra_tools
        if (
            "auto_compact_enabled" in file_settings.model_fields_set
            and "auto_compact_enabled" not in self.settings.model_fields_set
        ):
            merged.auto_compact_enabled = file_settings.auto_compact_enabled
        if (
            file_settings.max_context_length is not None
            and "max_context_length" not in self.settings.model_fields_set
        ):
            merged.max_context_length = file_settings.max_context_length
        if file_settings.ultra_mode and not self.settings.ultra_mode:
            merged.ultra_mode = file_settings.ultra_mode
        if file_settings.tool_call_timeout is not None and self.settings.tool_call_timeout is None:
            merged.tool_call_timeout = file_settings.tool_call_timeout
        if file_settings.tool_settings and not self.settings.tool_settings:
            merged.tool_settings = file_settings.tool_settings
        elif file_settings.tool_settings:
            for name, cfg in file_settings.tool_settings.items():
                merged.tool_settings.setdefault(name, {}).update(cfg)
        if file_settings.logging.level and merged.logging.level == "WARNING":
            merged.logging.level = file_settings.logging.level
        if file_settings.logging.file and not merged.logging.file:
            merged.logging.file = file_settings.logging.file
        if file_settings.hooks and not self.settings.hooks:
            merged.hooks = file_settings.hooks
        elif file_settings.hooks:
            for event_name, cfg_list in file_settings.hooks.items():
                existing = merged.hooks.setdefault(event_name, [])
                for item in cfg_list:
                    if item not in existing:
                        existing.append(item)

        configure_logging(self.cwd, merged.logging)

        # Keep a /model switch that ran before the first initialize() (late init).
        chosen = self._current_model_name
        if chosen is None or chosen not in merged.models:
            chosen = merged.default_model
        self._current_model_name = chosen
        active_api_config = merged.get_api_config(self._current_model_name)
        self._api_adapter = create_adapter(active_api_config)

        if not self.tools:
            self.tools = get_default_tools()

        from crabcode_core.tools.monitor import MonitorTool

        self._monitor_manager = next(
            (
                tool.manager
                for tool in self.tools
                if isinstance(tool, MonitorTool)
            ),
            None,
        )

        # Session storage is created lazily by _ensure_session_storage()
        # to avoid leaving empty session files when resume() is called.

        self._permission_manager = PermissionManager(
            settings=merged.permissions,
        )
        self._ai_reviewer = AiPermissionReviewer(
            settings=merged,
            default_api_config=active_api_config,
        )
        self._sync_client_permission_mode()
        from crabcode_core.hooks.manager import HookManager

        self._hook_manager = HookManager(merged.hooks)

        async def _push_agent_event(event: CoreEvent) -> None:
            if self._closed:
                return
            if self._foreground_turn_active:
                await self._agent_event_queue.put(event)
            else:
                await self._emit_background_event(event)

        async def _push_agent_completion(completion: AgentCompletion) -> None:
            if self._closed:
                return
            await self._agent_completion_queue.put(
                (self._lifecycle_generation, completion)
            )
            self._ensure_agent_completion_dispatcher()

        def _tools_provider() -> list[Tool]:
            return [tool for tool in self.tools if tool.name != "Agent"]

        def _adapter_provider(model_name: str | None) -> Any:
            selected_name = model_name if model_name is not None else self._current_model_name
            return create_adapter(self.settings.get_api_config(selected_name))

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
                logger.exception("Failed to load extra tool: %s", tool_path)

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

        def _persist_agents(snapshots: list[dict[str, Any]]) -> None:
            if self._session_storage:
                self._session_storage.write_agent_snapshots(snapshots)

        def _write_agent_transcript(agent_id: str, messages: list[Message]) -> None:
            if self._session_storage:
                self._session_storage.append_agent_messages(agent_id, messages)

        def _load_agent_transcript(agent_id: str) -> list[dict[str, Any]]:
            if not self._session_storage:
                return []
            return self._session_storage.load_agent_messages(agent_id)

        def _agent_transcript_path(agent_id: str) -> str:
            return str(get_agent_transcript_path(self.cwd, self.session_id, agent_id))

        self._agent_manager = AgentManager(
            settings=merged,
            agent_settings=merged.agent,
            tools_provider=_tools_provider,
            adapter_provider=_adapter_provider,
            event_sink=_push_agent_event,
            permission_manager=self._permission_manager,
            prompt_profile=self._prompt_profile,
            cwd=self.cwd,
            env=merged.env,
            session_id=self.session_id,
            completion_sink=_push_agent_completion,
            current_model_name=self._current_model_name,
            persistence_callback=_persist_agents,
            transcript_writer=_write_agent_transcript,
            transcript_loader=_load_agent_transcript,
            transcript_path_getter=_agent_transcript_path,
            hook_manager=self._hook_manager,
            lsp_manager=self._lsp_manager,
            ai_reviewer=self._ai_reviewer,
        )

        # Initialize TeamManager
        from crabcode_core.team.manager import TeamManager
        self._team_manager = TeamManager(
            agent_manager=self._agent_manager,
            settings=merged,
            event_sink=_push_agent_event,
            cwd=self.cwd,
            session_id=self.session_id,
        )
        self._agent_manager._team_manager = self._team_manager

        has_agent = any(isinstance(t, AgentTool) for t in self.tools)
        if not has_agent:
            sub_tools = list(self.tools)
            agent_cfg = merged.agent
            self.tools.append(AgentTool(
                manager=self._agent_manager,
                settings=merged.agent,
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

        # Initialize LSP manager (default on, can be disabled via settings)
        if merged.lsp is not False:
            try:
                self._lsp_manager = LSPManager(cwd=self.cwd, settings=merged)
                logger.info("LSP manager initialized with %d server(s)", len(self._lsp_manager.servers))
            except Exception:
                logger.warning("Failed to initialize LSP manager", exc_info=True)
                self._lsp_manager = None

        self._initialized = True

    def set_background_event_sink(
        self,
        sink: Callable[[CoreEvent], Awaitable[None]] | None,
    ) -> None:
        """Set the frontend sink used for events emitted outside a user turn."""
        self._background_event_sink = sink

    async def _emit_background_event(self, event: CoreEvent) -> None:
        if self._closed:
            return
        if self._background_event_sink is not None:
            await self._background_event_sink(event)
        else:
            await self._background_event_queue.put(event)

    async def next_background_event(self) -> CoreEvent:
        """Wait for an event produced while no foreground turn owns a stream."""
        return await self._background_event_queue.get()

    def _ensure_agent_completion_dispatcher(self) -> None:
        if self._closed:
            return
        if self._agent_completion_task is None or self._agent_completion_task.done():
            self._agent_completion_task = asyncio.create_task(
                self._dispatch_agent_completions()
            )

    async def enqueue_monitor_notification(
        self,
        notification: str,
        *,
        session_id: str,
    ) -> None:
        """Queue one Monitor event for a safe-boundary automatic continuation."""
        if self._closed or session_id != self.session_id:
            return
        await self._monitor_notification_queue.put(
            (self._lifecycle_generation, session_id, notification)
        )
        self._ensure_monitor_notification_dispatcher()

    def _ensure_monitor_notification_dispatcher(self) -> None:
        if self._closed:
            return
        if (
            self._monitor_notification_task is None
            or self._monitor_notification_task.done()
        ):
            self._monitor_notification_task = asyncio.create_task(
                self._dispatch_monitor_notifications()
            )

    async def _dispatch_monitor_notifications(self) -> None:
        """Batch ready monitor events and let the main model react to them."""
        try:
            while True:
                first = await self._monitor_notification_queue.get()
                # Coalesce bursts without turning every log line into a separate
                # model request. Every event remains present and ordered.
                await asyncio.sleep(0.05)
                batch = [first]
                batch_chars = len(first[2])
                while len(batch) < 100 and batch_chars < 500_000:
                    try:
                        item = self._monitor_notification_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    batch.append(item)
                    batch_chars += len(item[2])

                grouped: dict[tuple[int, str], list[str]] = {}
                for generation, session_id, notification in batch:
                    grouped.setdefault((generation, session_id), []).append(notification)

                for (generation, session_id), notifications in grouped.items():
                    if not self._lifecycle_matches(session_id, generation):
                        continue
                    async with self._turn_lock:
                        if not self._lifecycle_matches(session_id, generation):
                            continue
                        try:
                            async for event in self._send_message_impl(
                                "\n\n".join(notifications),
                                synthetic=True,
                                message_uuid=str(uuid.uuid4()),
                            ):
                                await self._emit_background_event(event)
                        except Exception as exc:
                            logger.exception("Automatic Monitor continuation failed")
                            try:
                                await self._emit_background_event(
                                    ErrorEvent(
                                        message=f"Monitor continuation failed: {exc}",
                                        recoverable=True,
                                        error_type="monitor_callback",
                                    )
                                )
                            except Exception:
                                logger.exception("Failed to publish Monitor callback error")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Monitor notification dispatcher failed")

    def _lifecycle_matches(self, session_id: str, generation: int) -> bool:
        return (
            not self._closed
            and self.session_id == session_id
            and self._lifecycle_generation == generation
        )

    async def _dispatch_agent_completions(self) -> None:
        try:
            while not self._closed:
                first = await self._agent_completion_queue.get()
                batch = [first]
                await asyncio.sleep(0)
                while True:
                    try:
                        batch.append(self._agent_completion_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                grouped: dict[
                    tuple[int, str | None, str],
                    list[AgentCompletion],
                ] = {}
                for generation, completion in batch:
                    if not self._lifecycle_matches(
                        completion.session_id,
                        generation,
                    ):
                        continue
                    delivery_key = (
                        f"injected:{completion.callback_message_id}"
                        if completion.callback_state == "injected"
                        and completion.callback_message_id
                        else "pending"
                    )
                    grouped.setdefault(
                        (generation, completion.parent_agent_id, delivery_key), []
                    ).append(completion)

                for (
                    generation,
                    parent_agent_id,
                    _delivery_key,
                ), completions in grouped.items():
                    if not self._lifecycle_matches(
                        completions[0].session_id,
                        generation,
                    ):
                        continue
                    try:
                        if parent_agent_id is None:
                            await self._continue_main_agent(
                                completions,
                                lifecycle_generation=generation,
                            )
                        else:
                            async with self._managed_callback_lock:
                                await self._continue_managed_parent(
                                    parent_agent_id,
                                    completions,
                                    lifecycle_generation=generation,
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception(
                            "Managed-agent callback group failed: parent=%s agents=%s",
                            parent_agent_id,
                            [completion.agent_id for completion in completions],
                        )
                        try:
                            await self._emit_background_event(
                                ErrorEvent(
                                    message=f"Managed-agent callback failed: {exc}",
                                    recoverable=True,
                                    error_type="agent_callback",
                                )
                            )
                        except Exception:
                            logger.exception(
                                "Failed to publish managed-agent callback error"
                            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Managed-agent completion dispatcher failed")

    def _format_agent_completion(
        self,
        completion: AgentCompletion,
        *,
        max_result_chars: int | None = None,
        notification_uuid: str | None = None,
    ) -> str:
        result = AgentManager.truncate_result(
            completion.final_result.strip() or "(no result)",
            max_result_chars or self.settings.agent.max_output_chars,
        )
        notification_status = (
            "stopped" if completion.status == "cancelled" else completion.status
        )
        summary = completion.error.strip() or completion.title
        fields = [
            ("session-id", completion.session_id),
            ("task-id", completion.agent_id),
            ("uuid", notification_uuid or ""),
            ("parent-agent-id", completion.parent_agent_id or ""),
            ("tool-use-id", completion.parent_tool_use_id or ""),
            ("callback-epoch", str(completion.callback_epoch)),
            ("status", notification_status),
            ("output-file", completion.transcript_path or ""),
            ("summary", summary),
            ("title", completion.title),
            ("subagent-type", completion.subagent_type),
            ("completed-at", completion.completed_at),
            ("transcript-path", completion.transcript_path or ""),
            ("error", completion.error),
        ]
        parts = ["<task-notification>"]
        parts.extend(
            f"<{name}>{escape(value)}</{name}>"
            for name, value in fields
            if value
        )
        if completion.usage:
            usage = ", ".join(
                f"{key}={value}" for key, value in completion.usage.items()
            )
            parts.append(f"<usage>{escape(usage)}</usage>")
        parts.extend(
            [
                f"<result>{escape(result)}</result>",
                "</task-notification>",
            ]
        )
        return "\n".join(parts)

    @staticmethod
    def _assistant_reply(messages: list[Message], message_id: str) -> Message | None:
        return find_assistant_reply(messages, message_id)

    @classmethod
    def _has_assistant_reply(cls, messages: list[Message], message_id: str) -> bool:
        return cls._assistant_reply(messages, message_id) is not None

    def _has_callback_delivery(
        self,
        completion: AgentCompletion,
        message_id: str,
    ) -> bool:
        return bool(
            self._session_storage
            and self._session_storage.has_callback_delivery(
                agent_id=completion.agent_id,
                callback_epoch=completion.callback_epoch,
                callback_message_id=message_id,
            )
        )

    def _record_callback_deliveries(
        self,
        completions: list[AgentCompletion],
        *,
        message_id: str,
        assistant_uuid: str,
    ) -> list[AgentCompletion]:
        if not self._session_storage:
            self._ensure_session_storage()
        if not self._session_storage:
            logger.error("Cannot acknowledge callback without durable session storage")
            return []
        return [
            completion
            for completion in completions
            if self._session_storage.record_callback_delivery(
                agent_id=completion.agent_id,
                callback_epoch=completion.callback_epoch,
                callback_message_id=message_id,
                assistant_uuid=assistant_uuid,
            )
        ]

    async def _continue_main_agent(
        self,
        completions: list[AgentCompletion],
        *,
        lifecycle_generation: int | None = None,
    ) -> None:
        if not completions:
            return
        expected_session_id = completions[0].session_id
        generation = (
            self._lifecycle_generation
            if lifecycle_generation is None
            else lifecycle_generation
        )
        async with self._turn_lock:
            if (
                not self._lifecycle_matches(expected_session_id, generation)
                or not self._agent_manager
            ):
                return
            current = [
                completion
                for completion in completions
                if completion.session_id == expected_session_id
            ]
            if not current:
                return

            message_ids: set[str] = set()
            for completion in current:
                snapshot = self._agent_manager.get_agent(completion.agent_id)
                if (
                    completion.callback_state == "injected"
                    and completion.callback_message_id
                ):
                    message_ids.add(completion.callback_message_id)
                elif (
                    snapshot is not None
                    and snapshot.callback_state == "injected"
                    and snapshot.callback_message_id
                    and snapshot.callback_epoch == completion.callback_epoch
                ):
                    message_ids.add(snapshot.callback_message_id)
            if len(message_ids) > 1:
                logger.error(
                    "Cannot merge callback completions with different message IDs: %s",
                    sorted(message_ids),
                )
                return
            message_id = next(iter(message_ids), None) or str(uuid.uuid4())
            awaiting_delivery: list[AgentCompletion] = []
            for completion in current:
                if self._has_callback_delivery(completion, message_id):
                    self._agent_manager.mark_callback_delivered(
                        completion.agent_id,
                        session_id=completion.session_id,
                        callback_epoch=completion.callback_epoch,
                    )
                else:
                    awaiting_delivery.append(completion)
            current = awaiting_delivery
            if not current:
                return

            existing_message_index = next(
                (
                    index
                    for index, message in enumerate(self.messages)
                    if message.uuid == message_id
                ),
                None,
            )
            if existing_message_index is not None:
                assistant_reply = self._assistant_reply(self.messages, message_id)
                if assistant_reply is not None:
                    durable = self._record_callback_deliveries(
                        current,
                        message_id=message_id,
                        assistant_uuid=assistant_reply.uuid,
                    )
                    delivery_session_id = current[0].session_id
                    for completion in durable:
                        self._agent_manager.mark_callback_delivered(
                            completion.agent_id,
                            session_id=delivery_session_id,
                            callback_epoch=completion.callback_epoch,
                        )
                    return

            injected: list[AgentCompletion] = []
            for completion in current:
                snapshot = self._agent_manager.get_agent(completion.agent_id)
                if (
                    snapshot is not None
                    and snapshot.callback_state == "injected"
                    and snapshot.callback_message_id == message_id
                    and snapshot.callback_epoch == completion.callback_epoch
                ):
                    injected.append(completion)
                elif self._agent_manager.mark_callback_injected(
                    completion.agent_id,
                    session_id=expected_session_id,
                    message_id=message_id,
                    callback_epoch=completion.callback_epoch,
                ):
                    injected.append(completion)
            if not injected:
                return

            result_budget = max(1, self.settings.agent.max_output_chars // len(injected))
            text = "\n\n".join(
                self._format_agent_completion(
                    completion,
                    max_result_chars=result_budget,
                    notification_uuid=message_id,
                )
                for completion in injected
            )
            if not self._lifecycle_matches(expected_session_id, generation):
                return
            terminal_event: TurnCompleteEvent | None = None
            try:
                async for event in self._send_message_impl(
                    text,
                    synthetic=True,
                    message_uuid=message_id,
                    reuse_existing_message=existing_message_index is not None,
                ):
                    if isinstance(event, TurnCompleteEvent):
                        terminal_event = event
                    await self._emit_background_event(event)
            except Exception:
                logger.exception("Automatic managed-agent continuation failed")

            assistant_reply = self._assistant_reply(self.messages, message_id)
            if (
                self._lifecycle_matches(expected_session_id, generation)
                and terminal_event is not None
                and assistant_reply is not None
            ):
                durable = self._record_callback_deliveries(
                    injected,
                    message_id=message_id,
                    assistant_uuid=assistant_reply.uuid,
                )
                delivery_session_id = current[0].session_id
                for completion in durable:
                    self._agent_manager.mark_callback_delivered(
                        completion.agent_id,
                        session_id=delivery_session_id,
                        callback_epoch=completion.callback_epoch,
                    )

    async def _continue_managed_parent(
        self,
        parent_agent_id: str,
        completions: list[AgentCompletion],
        *,
        lifecycle_generation: int | None = None,
    ) -> None:
        if not completions:
            return
        expected_session_id = completions[0].session_id
        generation = (
            self._lifecycle_generation
            if lifecycle_generation is None
            else lifecycle_generation
        )
        manager = self._agent_manager
        if (
            not self._lifecycle_matches(expected_session_id, generation)
            or manager is None
        ):
            return
        current = [
            completion
            for completion in completions
            if completion.session_id == expected_session_id
        ]
        if not current:
            return

        parent = manager.get_agent(parent_agent_id)
        if parent is None or parent.session_id != expected_session_id:
            await self._continue_main_agent(
                current,
                lifecycle_generation=generation,
            )
            return
        if parent.status in {"queued", "running"}:
            if manager.is_agent_active(parent_agent_id):
                await manager.wait_agent(parent_agent_id)
            else:
                await self._continue_main_agent(
                    current,
                    lifecycle_generation=generation,
                )
                return
        if not self._lifecycle_matches(expected_session_id, generation):
            return

        parent = manager.get_agent(parent_agent_id)
        if (
            parent is not None
            and parent.status in {"completed", "failed", "stopped", "cancelled"}
            and parent.callback_enabled
            and parent.callback_state in {"pending", "injected"}
        ):
            parent_completion = AgentCompletion.from_snapshot(parent)
            if parent.parent_agent_id is None:
                await self._continue_main_agent(
                    [parent_completion],
                    lifecycle_generation=generation,
                )
            else:
                await self._continue_managed_parent(
                    parent.parent_agent_id,
                    [parent_completion],
                    lifecycle_generation=generation,
                )
            parent = manager.get_agent(parent_agent_id)
            if parent is None or parent.callback_state != "delivered":
                await self._continue_main_agent(
                    current,
                    lifecycle_generation=generation,
                )
                return

        message_ids: set[str] = set()
        for completion in current:
            snapshot = manager.get_agent(completion.agent_id)
            if (
                completion.callback_state == "injected"
                and completion.callback_message_id
            ):
                message_ids.add(completion.callback_message_id)
            elif (
                snapshot is not None
                and snapshot.callback_state == "injected"
                and snapshot.callback_message_id
                and snapshot.callback_epoch == completion.callback_epoch
            ):
                message_ids.add(snapshot.callback_message_id)
        if len(message_ids) > 1:
            logger.error(
                "Cannot merge managed callbacks with different message IDs: %s",
                sorted(message_ids),
            )
            return
        message_id = next(iter(message_ids), None) or str(uuid.uuid4())
        awaiting_delivery: list[AgentCompletion] = []
        for completion in current:
            if self._has_callback_delivery(completion, message_id):
                manager.mark_callback_delivered(
                    completion.agent_id,
                    session_id=completion.session_id,
                    callback_epoch=completion.callback_epoch,
                )
            else:
                awaiting_delivery.append(completion)
        current = awaiting_delivery
        if not current:
            return

        injected: list[AgentCompletion] = []
        for completion in current:
            snapshot = manager.get_agent(completion.agent_id)
            if (
                snapshot is not None
                and snapshot.callback_state == "injected"
                and snapshot.callback_message_id == message_id
                and snapshot.callback_epoch == completion.callback_epoch
            ):
                injected.append(completion)
            elif manager.mark_callback_injected(
                completion.agent_id,
                session_id=expected_session_id,
                message_id=message_id,
                callback_epoch=completion.callback_epoch,
            ):
                injected.append(completion)
        if not injected:
            return

        result_budget = max(1, self.settings.agent.max_output_chars // len(injected))
        text = "\n\n".join(
            self._format_agent_completion(
                completion,
                max_result_chars=result_budget,
                notification_uuid=message_id,
            )
            for completion in injected
        )
        if not self._lifecycle_matches(expected_session_id, generation):
            return
        if not await manager.send_input(
            parent_agent_id,
            text,
            message_id=message_id,
            message_origin="task-notification",
        ):
            await self._continue_main_agent(
                injected,
                lifecycle_generation=generation,
            )
            return

        if manager.is_agent_active(parent_agent_id):
            await manager.wait_agent(parent_agent_id)
        if not self._lifecycle_matches(expected_session_id, generation):
            return
        parent = manager.get_agent(parent_agent_id)
        assistant_reply = manager.get_agent_reply(parent_agent_id, message_id)
        if (
            parent is None
            or parent.status not in {"completed", "failed", "stopped", "cancelled"}
            or assistant_reply is None
        ):
            return
        durable = self._record_callback_deliveries(
            injected,
            message_id=message_id,
            assistant_uuid=assistant_reply.uuid,
        )
        delivery_session_id = injected[0].session_id
        for completion in durable:
            manager.mark_callback_delivered(
                completion.agent_id,
                session_id=delivery_session_id,
                callback_epoch=completion.callback_epoch,
            )

    def _ensure_session_storage(self) -> None:
        """Lazily create session storage on first real use.

        This avoids creating empty session files when resume() will be called
        right after initialize().
        """
        if self._session_storage is not None:
            return
        from crabcode_core.session.storage import SessionStorage, generate_session_id

        if not self.session_id:
            self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)
        if self._agent_manager:
            self._agent_manager.update_session(env=self.settings.env, session_id=self.session_id)
        active_cfg = self.settings.get_api_config(self._current_model_name)
        self._session_storage.write_meta(
            model=active_cfg.model or "",
            provider=active_cfg.provider or "",
        )

    def _maybe_generate_title(self) -> None:
        """Fire-and-forget task to generate an LLM title after the first turn."""
        if self._title_generation_task is not None:
            return
        if not self._session_storage or not self._api_adapter:
            return
        meta = self._session_storage.meta
        # Only generate if title is still the truncated first_user_message
        title = meta.get("title", "")
        first_msg = meta.get("first_user_message", "")
        if not first_msg:
            return
        if title and title != first_msg[:200]:
            return

        first_assistant_text = ""
        for msg in self.messages:
            if msg.role.value == "assistant":
                first_assistant_text = msg.text_content[:500]
                break

        storage = self._session_storage
        adapter = self._api_adapter

        async def _gen() -> None:
            try:
                from crabcode_core.session.title_gen import generate_title
                new_title = await generate_title(first_msg, first_assistant_text, adapter)
                if new_title:
                    storage.update_title(new_title)
            except Exception:
                logger.debug("Background title generation failed", exc_info=True)

        self._title_generation_task = asyncio.create_task(_gen())

    async def close(self) -> None:
        """Release session-scoped resources."""
        if self._closed:
            return
        self._closed = True
        self._lifecycle_generation += 1
        self._drain_session_queues()

        if self._agent_completion_task is not None:
            self._agent_completion_task.cancel()
            try:
                await self._agent_completion_task
            except asyncio.CancelledError:
                pass
            self._agent_completion_task = None

        if self._monitor_notification_task is not None:
            self._monitor_notification_task.cancel()
            try:
                await self._monitor_notification_task
            except asyncio.CancelledError:
                pass
            self._monitor_notification_task = None

        if self._agent_manager is not None:
            await self._agent_manager.close()

        if self._team_manager is not None:
            await self._team_manager.close()

        if self._lsp_manager is not None:
            try:
                await self._lsp_manager.shutdown()
            except Exception:
                logger.warning("Failed to shut down LSP manager", exc_info=True)
            self._lsp_manager = None

        if self._mcp_manager is not None:
            await self._mcp_manager.disconnect_all()

        for tool in reversed(self.tools):
            try:
                await tool.close()
            except Exception:
                logger.warning("Failed to close tool %s", tool.name, exc_info=True)

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
        images: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[CoreEvent, None]:
        """Serialize turns and settle a queued manual compact at a safe boundary."""
        await self.initialize()
        if self._agent_completion_task is not None and self._agent_completion_task.done():
            try:
                error = self._agent_completion_task.exception()
            except asyncio.CancelledError:
                error = None
            if error is not None:
                logger.error("Managed-agent completion dispatcher stopped: %s", error)
            self._agent_completion_task = None
            if not self._agent_completion_queue.empty():
                self._ensure_agent_completion_dispatcher()
        if (
            self._monitor_notification_task is not None
            and self._monitor_notification_task.done()
        ):
            try:
                monitor_error = self._monitor_notification_task.exception()
            except asyncio.CancelledError:
                monitor_error = None
            if monitor_error is not None:
                logger.error("Monitor notification dispatcher stopped: %s", monitor_error)
            self._monitor_notification_task = None
            if not self._monitor_notification_queue.empty():
                self._ensure_monitor_notification_dispatcher()
        async with self._turn_lock:
            self._foreground_turn_active = True
            try:
                async for event in self._send_message_impl(
                    text,
                    max_turns=max_turns,
                    images=images,
                ):
                    yield event
            finally:
                self._foreground_turn_active = False
                if self._pending_manual_compact is not None:
                    instructions = self._pending_manual_compact
                    self._pending_manual_compact = None
                    await self._compact_now(
                        trigger="manual",
                        custom_instructions=instructions or None,
                    )

    async def _send_message_impl(
        self,
        text: str,
        max_turns: int = 0,
        images: list[dict[str, Any]] | None = None,
        *,
        synthetic: bool = False,
        message_uuid: str | None = None,
        reuse_existing_message: bool = False,
    ) -> AsyncGenerator[CoreEvent, None]:
        """Send a user message and stream back events.

        Args:
            text: The user's text message.
            max_turns: Maximum agentic turns (0 = unlimited).
            images: Optional list of image attachments. Each dict should have
                    ``media_type`` (e.g. "image/png") and ``data`` (base64-encoded).
        """
        await self.initialize()
        self._ensure_session_storage()
        self._abort_controller.clear()

        from crabcode_core.prompts.context import get_system_context, get_user_context
        from crabcode_core.prompts.profile import PromptProfile
        from crabcode_core.prompts.system import get_system_prompt
        from crabcode_core.query.loop import QueryParams, query_loop
        from crabcode_core.types.event import CompactEvent, ErrorEvent, TurnCompleteEvent
        from crabcode_core.types.message import create_user_message
        from crabcode_core.types.tool import ToolContext

        user_msg_content = text
        hook_blocked_reason = ""
        if self._hook_manager and not synthetic:
            hook_result = await self._hook_manager.run(
                "user_prompt_submit",
                {"user_text": text},
                cwd=self.cwd,
                env=self.settings.env,
            )
            if hook_result.feedback:
                payload = "\n\n".join(
                    f"<user-prompt-submit-hook>\n{feedback}\n</user-prompt-submit-hook>"
                    for feedback in hook_result.feedback
                    if feedback
                )
                if payload:
                    user_msg_content = f"{text}\n\n{payload}" if text else payload
            if hook_result.blocked:
                hook_blocked_reason = "; ".join(hook_result.details or []) or "blocked by user_prompt_submit hook"

        # Build user message content — text + optional image blocks
        existing_message = next(
            (
                message
                for message in self.messages
                if message_uuid is not None and message.uuid == message_uuid
            ),
            None,
        )
        if reuse_existing_message:
            if existing_message is None:
                raise RuntimeError(
                    f"Cannot reuse missing durable message {message_uuid}"
                )
            user_msg = existing_message
            if self.messages[-1] is not user_msg:
                self.messages.remove(user_msg)
                self.messages.append(user_msg)
        elif images:
            from crabcode_core.types.message import ImageBlock as _ImageBlock
            content_blocks: list[Any] = []
            if user_msg_content:
                from crabcode_core.types.message import TextBlock as _TextBlock
                content_blocks.append(_TextBlock(text=user_msg_content))
            for img in images:
                content_blocks.append(_ImageBlock(
                    source={
                        "type": "base64",
                        "media_type": img.get("media_type", "image/png"),
                        "data": img.get("data", ""),
                    }
                ))
            user_msg = create_user_message(content=content_blocks)
            self.messages.append(user_msg)
        else:
            kwargs: dict[str, Any] = {}
            if message_uuid:
                kwargs["uuid"] = message_uuid
            if synthetic:
                kwargs["origin"] = "task-notification"
            user_msg = create_user_message(content=user_msg_content, **kwargs)
            self.messages.append(user_msg)

        if hook_blocked_reason:
            if self._session_storage:
                self._session_storage.append_message(user_msg)
            yield ErrorEvent(
                message=f"Prompt blocked by hook: {hook_blocked_reason}",
                recoverable=True,
                error_type="hook",
            )
            return

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
            if not synthetic and not self._session_storage.meta.get("first_user_message"):
                active_api_cfg = self.settings.get_api_config(self._current_model_name)
                self._session_storage.write_meta(
                    model=active_api_cfg.model or "",
                    provider=active_api_cfg.provider or "",
                    first_user_message=text,
                )

        active_api_cfg = self.settings.get_api_config(self._current_model_name)
        if hasattr(self._api_adapter, "resolve_context_window"):
            resolved_context_window = await self._api_adapter.resolve_context_window()
        else:
            from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window
            resolved_context_window = (
                active_api_cfg.context_window
                or lookup_context_window(active_api_cfg.model)
                or DEFAULT_CONTEXT_WINDOW
            )

        tool_names = [t.name for t in self.tools if t.is_enabled]
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
            agent_mode=self._agent_mode,
            ultra_mode=self.settings.ultra_mode,
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
            agent_id=None,
            agent_depth=0,
            agent_manager=self._agent_manager,
            lsp_manager=self._lsp_manager,
            team_manager=self._team_manager,
            session=self,
        )

        # Sync SwitchModeTool's current_mode so its prompt and validation
        # reflect the actual mode the agent is running in.
        from crabcode_core.tools.switch_mode import SwitchModeTool
        for tool in self.tools:
            if isinstance(tool, SwitchModeTool):
                tool.current_mode = self._agent_mode
                break

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
            hook_manager=self._hook_manager,
            agent_mode=self._agent_mode,
            api_config=active_api_cfg,
            context_window=resolved_context_window,
            ai_reviewer=self._ai_reviewer,
            tool_call_timeout=self.settings.tool_call_timeout,
            auto_compact_enabled=self.settings.auto_compact_enabled,
            compact_threshold=self.settings.max_context_length,
            reply_to_uuid=message_uuid if synthetic else None,
        )

        merged_events: asyncio.Queue[CoreEvent | None] = asyncio.Queue()

        async def _produce_main_events() -> None:
            try:
                async for event in query_loop(params):
                    await merged_events.put(event)
            except asyncio.CancelledError:
                await merged_events.put(TurnCompleteEvent(reason="interrupted"))
                raise
            except Exception:
                logger.exception("query_loop crashed")
                await merged_events.put(
                    ErrorEvent(message="Internal error in query loop", recoverable=False, error_type="internal")
                )
            finally:
                await merged_events.put(None)

        async def _forward_agent_events() -> None:
            while True:
                event = await self._agent_event_queue.get()
                await merged_events.put(event)

        async def _watch_abort() -> None:
            await self._abort_controller.wait()
            producer.cancel()

        producer = asyncio.create_task(_produce_main_events())
        agent_forwarder = asyncio.create_task(_forward_agent_events())
        abort_watcher = asyncio.create_task(_watch_abort())

        try:
            while True:
                event = await merged_events.get()
                if event is None:
                    break
                if isinstance(event, CompactEvent) and event.agent_id is None:
                    # Commit any full in-flight messages before the compact boundary,
                    # then use the event's frozen projection. Reading params here is
                    # racy because the producer may already be processing the retry.
                    source_messages = event.source_messages or []
                    checkpoint_messages = event.checkpoint_messages or list(params.messages)
                    if self._session_storage:
                        for msg in source_messages:
                            self._session_storage.append_message(msg)
                    self.messages = checkpoint_messages
                    from crabcode_core.compact.compact import estimate_token_count
                    self._persist_compaction(
                        self.messages,
                        trigger=event.trigger,
                        messages_before=event.messages_before,
                        estimated_tokens_before=estimate_token_count(source_messages),
                    )
                    event.source_messages = None
                    event.checkpoint_messages = None
                if isinstance(event, TurnCompleteEvent):
                    self.messages = params.messages
                    if event.context_used_tokens or event.context_window_tokens:
                        self.last_context_used_tokens = event.context_used_tokens
                        self.last_context_window_tokens = event.context_window_tokens

                    if self._session_storage:
                        # UUID de-duplication makes this safe across compaction, where
                        # list indices no longer correspond to the pre-loop history.
                        for msg in self.messages:
                            self._session_storage.append_message(msg)
                        total_tokens = event.usage.get("input_tokens", 0) + event.usage.get("output_tokens", 0)
                        if total_tokens > 0:
                            self._session_storage.record_tokens(total_tokens)
                        self._session_storage.record_message_count(len(self.messages))
                        self._session_storage.record_context_usage(
                            self.last_context_used_tokens,
                            self.last_context_window_tokens,
                        )
                        self._maybe_generate_title()
                yield event
        finally:
            abort_watcher.cancel()
            agent_forwarder.cancel()
            producer.cancel()

    async def respond_permission(self, response: PermissionResponseEvent) -> None:
        if self._agent_manager and response.agent_id:
            if await self._agent_manager.route_permission(response):
                return
        await self._permission_queue.put(response)

    async def respond_choice(self, response: ChoiceResponseEvent) -> None:
        if self._agent_manager and response.agent_id:
            if await self._agent_manager.route_choice(response):
                return
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

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[Any]) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _drain_agent_completion_queue(self) -> None:
        self._drain_queue(self._agent_completion_queue)

    def _drain_background_event_queue(self) -> None:
        self._drain_queue(self._background_event_queue)

    def _drain_monitor_notification_queue(self) -> None:
        self._drain_queue(self._monitor_notification_queue)

    def _drain_session_queues(self) -> None:
        self._drain_agent_completion_queue()
        self._drain_monitor_notification_queue()
        self._drain_background_event_queue()
        self._drain_queue(self._agent_event_queue)
        self._drain_queue(self._permission_queue)
        self._drain_queue(self._choice_queue)

    def new_session(self) -> str:
        """Start a fresh session, preserving tools and config. Returns the new session ID."""
        from crabcode_core.session.storage import SessionStorage, generate_session_id

        if self._turn_lock.locked():
            raise RuntimeError("Cannot start a new session while a turn is still running")
        self._lifecycle_generation += 1
        if self._monitor_manager and self.session_id:
            self._monitor_manager.cancel_session_now(
                self.session_id,
                "session replaced",
            )
        if self._agent_manager:
            self._agent_manager.abandon_active_agents("session replaced")
            self._agent_manager.restore_snapshots([])
        self._drain_session_queues()
        self.messages.clear()
        self.compact_count = 0
        self._persisted_compact_summaries.clear()
        self._pending_manual_compact = None
        self.session_id = generate_session_id()
        self._session_storage = SessionStorage(self.cwd, self.session_id)
        # Write meta for the new session
        if self._initialized:
            active_api_cfg = self.settings.get_api_config(self._current_model_name)
            self._session_storage.write_meta(
                model=active_api_cfg.model or "",
                provider=active_api_cfg.provider or "",
            )
        if self._agent_manager:
            self._agent_manager.update_session(env=self.settings.env, session_id=self.session_id)
        return self.session_id

    def _persist_compaction(
        self,
        messages: list[Message],
        *,
        trigger: str,
        messages_before: int,
        estimated_tokens_before: int = 0,
    ) -> bool:
        """Persist a compact snapshot once and update session metadata."""
        if not messages or not messages[0].is_compact_summary:
            return False
        summary_message = messages[0]
        if summary_message.uuid in self._persisted_compact_summaries:
            return False

        from crabcode_core.compact.compact import compact_summary_text, estimate_token_count

        self._ensure_session_storage()
        if self._session_storage:
            self._session_storage.append_compaction(
                messages,
                trigger=trigger,
                messages_before=max(0, messages_before),
                estimated_tokens_before=estimated_tokens_before,
                estimated_tokens_after=estimate_token_count(messages),
            )
            summary = compact_summary_text(summary_message)
            if summary:
                self._session_storage.update_summary(summary)
        self._persisted_compact_summaries.add(summary_message.uuid)
        self.compact_count += 1
        return True

    async def _compact_now(
        self,
        *,
        trigger: str,
        custom_instructions: str | None = None,
    ) -> bool:
        from crabcode_core.compact.compact import compact_conversation, compact_summary_text

        old_count = len(self.messages)
        if old_count < 4:
            return False

        from crabcode_core.compact.compact import estimate_token_count
        estimated_tokens_before = estimate_token_count(self.messages)

        if self._hook_manager:
            pre = await self._hook_manager.run(
                "pre_compact",
                {
                    "session_id": self.session_id,
                    "trigger": trigger,
                    "custom_instructions": custom_instructions or "",
                },
                cwd=self.cwd,
                env=self.settings.env,
            )
            if pre.blocked:
                return False

        context_window = 0
        if hasattr(self._api_adapter, "resolve_context_window"):
            context_window = await self._api_adapter.resolve_context_window()
        result = await compact_conversation(
            self.messages,
            api_adapter=self._api_adapter,
            custom_instructions=custom_instructions,
            context_window=context_window,
        )
        if not result:
            return False

        self.messages = result
        self._persist_compaction(
            result,
            trigger=trigger,
            messages_before=old_count,
            estimated_tokens_before=estimated_tokens_before,
        )
        if self._hook_manager:
            await self._hook_manager.run(
                "post_compact",
                {
                    "session_id": self.session_id,
                    "trigger": trigger,
                    "compact_summary": compact_summary_text(result[0]),
                },
                cwd=self.cwd,
                env=self.settings.env,
            )
        return True

    async def compact(self, custom_instructions: str | None = None) -> bool:
        """Run manual compaction now, or coalesce it at the active turn boundary."""
        await self.initialize()
        instructions = (custom_instructions or "").strip()
        if self._turn_lock.locked():
            self._pending_manual_compact = instructions
            return True
        async with self._turn_lock:
            return await self._compact_now(
                trigger="manual",
                custom_instructions=instructions or None,
            )

    def checkpoint(self, label: str = "") -> str | None:
        """Create a checkpoint at the current conversation position.

        Also creates a file-system snapshot so that ``/revert`` can later
        restore both the conversation *and* the files.
        """
        if not self._session_storage:
            return None
        # Create a file-system snapshot alongside the conversation checkpoint
        snapshot_id: str | None = None
        try:
            from crabcode_core.snapshot.tracker import create_full_snapshot
            snapshot_id = create_full_snapshot(self.cwd, self.session_id, label=label)
        except Exception:
            logger.debug("Failed to create file snapshot for checkpoint", exc_info=True)
        return self._session_storage.create_checkpoint(
            self.messages, label=label, snapshot_id=snapshot_id,
        )

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List checkpoints for the current session."""
        if not self._session_storage:
            return []
        return self._session_storage.list_checkpoints()

    def rollback(self, checkpoint_id: str) -> bool:
        """Rollback conversation to a checkpoint (conversation only, no file restore).

        For file + conversation restore, use :meth:`revert` instead.
        """
        if not self._session_storage:
            return False
        idx = self._session_storage.rollback_to_checkpoint(checkpoint_id)
        if idx is None:
            return False
        self.messages = self.messages[: idx + 1]
        if self._session_storage:
            self._session_storage.record_message_count(len(self.messages))
        return True

    def revert(self, checkpoint_id: str) -> dict[str, Any]:
        """Revert both files and conversation to a checkpoint.

        Returns a dict with keys:
          - ``success``: bool
          - ``files_restored``: list[str] — files that were restored
          - ``messages_rolled_back``: int — how many messages were removed
          - ``snapshot_id``: str | None — the file snapshot that was restored
          - ``warning``: str | None — any warning message

        If the checkpoint has no file snapshot, only the conversation is
        rolled back (equivalent to ``rollback()``) and a warning is set.
        """
        result: dict[str, Any] = {
            "success": False,
            "files_restored": [],
            "messages_rolled_back": 0,
            "snapshot_id": None,
            "warning": None,
        }
        if not self._session_storage:
            return result

        # Look up the checkpoint to get its snapshot_id
        from crabcode_core.session.meta_db import SessionMetaStore
        store = SessionMetaStore()
        cp = store.get_checkpoint(checkpoint_id)
        store.close()

        if not cp or cp["session_id"] != self.session_id:
            return result

        snapshot_id = cp.get("snapshot_id")
        result["snapshot_id"] = snapshot_id

        # Restore file system if snapshot exists
        files_restored: list[str] = []
        if snapshot_id:
            try:
                from crabcode_core.snapshot.tracker import restore_snapshot
                files_restored = restore_snapshot(self.cwd, snapshot_id)
                result["files_restored"] = files_restored
            except Exception:
                logger.warning("Failed to restore snapshot %s", snapshot_id, exc_info=True)
                result["warning"] = "File restore failed; only conversation was rolled back"
        else:
            result["warning"] = "No file snapshot for this checkpoint; only conversation was rolled back"

        # Roll back conversation
        old_count = len(self.messages)
        idx = self._session_storage.rollback_to_checkpoint(checkpoint_id)
        if idx is not None:
            self.messages = self.messages[: idx + 1]
            self._session_storage.record_message_count(len(self.messages))
            result["messages_rolled_back"] = old_count - len(self.messages)

        result["success"] = True
        return result

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

        api_config = self.settings.models[name]
        self._api_adapter = create_adapter(api_config)
        self._current_model_name = name
        if self._agent_manager:
            self._agent_manager.set_current_model(name)

        return True

    def _sync_client_permission_mode(self) -> None:
        """Apply VS Code / client footer permission override when not in plan mode."""
        if self._permission_manager is None or self._agent_mode == "plan":
            return
        if self._client_permission_mode_override is None:
            self._permission_manager.reset_mode()
            return
        if self._client_permission_mode_override not in (
            "ask",
            "run_everything",
            "bypassPermissions",
            "ai_review",
            "aiReview",
        ):
            return
        from crabcode_core.permissions.manager import mode_from_default_mode

        self._permission_manager.mode = mode_from_default_mode(
            self._client_permission_mode_override,
        )

    def set_client_permission_mode(self, mode: str) -> bool:
        """Set tool permission behavior from the extension chat footer.

        ``default`` clears the client override and follows the loaded settings.
        ``ask`` uses normal allow/ask/deny rules. ``run_everything`` auto-approves
        tools. ``ai_review`` lets an AI reviewer decide whether to allow, ask,
        or deny.
        """
        if mode not in (
            "ask",
            "default",
            "run_everything",
            "bypassPermissions",
            "ai_review",
            "aiReview",
        ):
            return False
        self._client_permission_mode_override = None if mode == "default" else mode
        self._sync_client_permission_mode()
        return True

    def switch_mode(self, mode: str) -> bool:
        """Switch between 'agent' and 'plan' mode.

        In plan mode, only read-only tools are available and the agent
        is instructed to produce a structured plan instead of executing changes.
        """
        if mode not in ("agent", "plan"):
            return False
        if mode == self._agent_mode:
            return True
        self._agent_mode = mode
        if self._permission_manager:
            from crabcode_core.permissions.manager import PermissionMode

            if mode == "plan":
                self._saved_permission_mode = self._permission_manager.mode
                self._permission_manager.mode = PermissionMode.PLAN
            else:
                self._permission_manager.mode = (
                    self._saved_permission_mode
                    if self._saved_permission_mode is not None
                    else PermissionMode.DEFAULT
                )
                self._saved_permission_mode = None
                self._sync_client_permission_mode()
        return True

    @property
    def agent_mode(self) -> str:
        return self._agent_mode

    @property
    def current_plan(self) -> Any:
        from crabcode_core.plan.types import ExecutionPlan
        return self._current_plan

    def set_plan(self, plan: Any) -> None:
        self._current_plan = plan

    async def spawn_agent(
        self,
        *,
        prompt: str,
        subagent_type: str = "generalPurpose",
        name: str | None = None,
        model_profile: str | None = None,
        parent_agent_id: str | None = None,
        parent_tool_use_id: str | None = None,
        depth: int = 1,
        callback: bool = False,
    ) -> str:
        await self.initialize()
        if not self._agent_manager:
            raise RuntimeError("Agent manager is not initialized")
        if callback:
            self._ensure_session_storage()
        return await self._agent_manager.spawn_agent(
            prompt=prompt,
            subagent_type=subagent_type,
            name=name,
            model_profile=model_profile,
            parent_agent_id=parent_agent_id,
            parent_tool_use_id=parent_tool_use_id,
            depth=depth,
            callback=callback,
        )

    def get_agent(self, agent_id: str) -> AgentSnapshot | None:
        if not self._agent_manager:
            return None
        return self._agent_manager.get_agent(agent_id)

    def list_agents(self) -> list[AgentSnapshot]:
        if not self._agent_manager:
            return []
        return self._agent_manager.list_agents()

    def list_monitor_tasks(self) -> list[Any]:
        if not self._monitor_manager:
            return []
        return self._monitor_manager.list_tasks(self.session_id or None)

    async def stop_background_task(self, task_id: str) -> bool:
        await self.initialize()
        if self._monitor_manager:
            monitor_id = self._monitor_manager.resolve_task_id(task_id)
            if monitor_id and await self._monitor_manager.stop_task(monitor_id):
                return True
        if self._agent_manager:
            matches = [
                snapshot.agent_id
                for snapshot in self._agent_manager.list_agents()
                if snapshot.agent_id.startswith(task_id)
            ]
            if len(matches) == 1:
                return await self._agent_manager.cancel_agent(matches[0])
        return False

    async def wait_agent(
        self, agent_id: str | list[str], timeout_ms: int | None = None
    ) -> AgentSnapshot | None:
        await self.initialize()
        if not self._agent_manager:
            return None
        if isinstance(agent_id, list):
            return await self._agent_manager.wait_any(agent_id, timeout_ms=timeout_ms)
        return await self._agent_manager.wait_agent(agent_id, timeout_ms=timeout_ms)

    async def cancel_agent(self, agent_id: str) -> bool:
        await self.initialize()
        if not self._agent_manager:
            return False
        return await self._agent_manager.cancel_agent(agent_id)

    async def send_agent_input(
        self,
        agent_id: str,
        prompt: str,
        *,
        interrupt: bool = False,
    ) -> bool:
        await self.initialize()
        if not self._agent_manager:
            return False
        return await self._agent_manager.send_input(
            agent_id,
            prompt,
            interrupt=interrupt,
        )

    async def resume(self, session_id: str) -> bool:
        """Resume a previous session after any active foreground/background turn."""
        async with self._turn_lock:
            return await self._resume_session(session_id)

    async def _resume_session(self, session_id: str) -> bool:
        """Resume a previous session by loading its messages."""
        from crabcode_core.session.storage import SessionStorage
        storage = SessionStorage(self.cwd, session_id)
        raw_messages = storage.load_messages()
        agent_snapshots = storage.load_agent_snapshots()

        if not raw_messages and not storage.meta and not agent_snapshots:
            # Try cross-project lookup via SQLite
            cross = SessionStorage.from_session_id(session_id)
            if cross is not None:
                storage = cross
                self.cwd = storage.cwd
                raw_messages = storage.load_messages()
                agent_snapshots = storage.load_agent_snapshots()

        if not raw_messages and not storage.meta and not agent_snapshots:
            return False

        self._lifecycle_generation += 1
        lifecycle_generation = self._lifecycle_generation
        if self._monitor_manager and self.session_id:
            self._monitor_manager.cancel_session_now(
                self.session_id,
                "session resumed elsewhere",
            )
        if self._agent_manager:
            self._agent_manager.abandon_active_agents("session resumed elsewhere")
        self.session_id = session_id
        self._session_storage = storage
        self._drain_session_queues()
        self.messages.clear()
        self.compact_count = storage.compact_count
        self._persisted_compact_summaries.clear()
        pending_completions: list[AgentCompletion] = []
        if self._agent_manager:
            self._agent_manager.update_session(env=self.settings.env, session_id=self.session_id)
            pending_completions = self._agent_manager.restore_snapshots(agent_snapshots)

        # Restore context usage so the gateway can report it after session switch
        if storage.last_context_used_tokens or storage.last_context_window_tokens:
            self.last_context_used_tokens = storage.last_context_used_tokens
            self.last_context_window_tokens = storage.last_context_window_tokens

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
                                logger.debug("Failed to parse stored session timestamp: %r", ts, exc_info=True)
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
                logger.warning("Failed to sync resumed session metadata to SQLite", exc_info=True)

        for raw in raw_messages:
            message = message_from_entry(raw)
            if message is not None:
                self.messages.append(message)

        if self.messages and self.messages[0].is_compact_summary:
            self._persisted_compact_summaries.add(self.messages[0].uuid)

        for completion in pending_completions:
            await self._agent_completion_queue.put(
                (lifecycle_generation, completion)
            )
        if pending_completions:
            self._ensure_agent_completion_dispatcher()

        return True
