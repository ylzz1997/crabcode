"""Interactive REPL — rich terminal UI with streaming, Markdown, and tool rendering."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_or_none, in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    VerticalAlign,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from crabcode_cli.banner import print_banner
from crabcode_core.events import CoreSession
from crabcode_core.logging_utils import get_logger
from crabcode_core.types.config import (
    REASONING_EFFORT_LEVELS,
    CrabCodeSettings,
    DisplaySettings,
)
from crabcode_core.types.message import Message, MessageRole
from crabcode_core.utf8_sanitize import safe_utf8_str
from crabcode_core.types.event import (
    AgentOutputEvent,
    AgentStateEvent,
    ChoiceRequestEvent,
    ChoiceResponseEvent,
    CompactEvent,
    ErrorEvent,
    ModeChangeEvent,
    PermissionRequestEvent,
    PermissionResponseEvent,
    PeerMessageEvent,
    PlanReadyEvent,
    ScheduleRunEvent,
    StreamModeEvent,
    StreamTextEvent,
    SteeringAppliedEvent,
    TaskUpdateEvent,
    TeamMessageEvent,
    TeamStateEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
)

# Module-level display settings, set during run_repl()
_display_settings: DisplaySettings | None = None
logger = get_logger(__name__)


def _supports_ansi_output() -> bool:
    """Return True when stdout is an interactive ANSI-capable terminal."""
    if os.getenv("CRABCODE_PLAIN_OUTPUT") or os.getenv("NO_COLOR"):
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )


_ANSI_ENABLED = _supports_ansi_output()
console = Console(no_color=not _ANSI_ENABLED, force_terminal=_ANSI_ENABLED)


def _format_token_count(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}k"
    return str(tokens)


def _format_percent(percent: float) -> str:
    rounded = round(percent)
    if abs(percent - rounded) < 0.05:
        return f"{rounded}%"
    return f"{percent:.1f}%"


def _cache_usage_text(usage: dict[str, Any]) -> str | None:
    if "cache_read_tokens" not in usage and "cache_write_tokens" not in usage:
        return None
    cache_read = max(0, int(usage.get("cache_read_tokens", 0) or 0))
    cache_write = max(0, int(usage.get("cache_write_tokens", 0) or 0))
    total_input = max(
        0,
        int(usage.get("total_input_tokens", usage.get("input_tokens", 0)) or 0),
    )
    hit_rate = cache_read / total_input * 100 if total_input else 0.0
    parts = [
        f"Cache: {_format_percent(hit_rate)} hit",
        f"read {_format_token_count(cache_read)}",
    ]
    if "cache_write_tokens" in usage:
        parts.append(f"write {_format_token_count(cache_write)}")
    return " · ".join(parts)


def _render_context_usage(event: TurnCompleteEvent) -> None:
    used = max(0, int(getattr(event, "context_used_tokens", 0) or 0))
    window = max(0, int(getattr(event, "context_window_tokens", 0) or 0))
    cache_text = _cache_usage_text(event.usage)
    if not used and not window and not cache_text:
        return

    parts: list[str] = []
    if window:
        used_percent = max(0.0, float(getattr(event, "context_used_percent", 0.0) or 0.0))
        remaining_percent = max(0.0, 100.0 - used_percent)
        parts.append(
            "Context: "
            f"{_format_percent(used_percent)} used "
            f"({_format_percent(remaining_percent)} remaining) · "
            f"{_format_token_count(used)} tokens used of {_format_token_count(window)}"
        )
    elif used:
        parts.append(f"Context: {_format_token_count(used)} tokens used (window unknown)")
    if cache_text:
        parts.append(cache_text)
    console.print(f"  [dim]{' · '.join(parts)}[/]")


# Slash commands with their arguments for auto-completion
_SLASH_COMMANDS: dict[str, list[str]] = {
    "/help": [],
    "/goal": ["set", "edit", "pause", "resume", "complete", "blocked", "clear"],
    "/plan": [],
    "/agent": [],
    "/plan-status": [],
    "/agents": [],
    "/peers": [],
    "/tasks": ["stop"],
    "/agent-log": [],
    "/agent-send": [],
    "/wait": [],
    "/cancel-agent": [],
    "/team": [
        "list",
        "create",
        "status",
        "messages",
        "tasks",
        "spawn",
        "message",
        "broadcast",
        "task-add",
        "task-claim",
        "task-complete",
        "shutdown",
    ],
    "/schedule": [
        "list",
        "show",
        "runs",
        "create",
        "pause",
        "resume",
        "run",
        "cancel",
    ],
    "/status": [],
    "/effort": list(REASONING_EFFORT_LEVELS),
    "/ultra": ["true", "false"],
    "/logs": ["-f", "--follow", "--clear", "--tail"],
    "/model": [],  # Dynamic: model names
    "/new": [],
    "/compact": [],
    "/clear": [],
    "/sessions": [],
    "/recent": [],
    "/search": [],  # Dynamic: search query
    "/archive": [],
    "/export": [],
    "/stats": [],
    "/checkpoint": [],
    "/checkpoints": [],
    "/rollback": [],
    "/revert": [],
    "/undo": [],
    "/resume": [],  # Dynamic: session IDs
    "/exit": [],
    "/quit": [],
    "/image": [],  # Dynamic: file path
}


class _CrabCodeCompleter(Completer):
    """Auto-completer for slash commands and their arguments."""

    def __init__(self, session: "CoreSession" | None = None) -> None:
        self._session = session

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        word_before_cursor = document.get_word_before_cursor(WORD=False)

        # Only complete after /
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower() if parts else ""

        # First token must include leading "/"; get_word_before_cursor(WORD=False)
        # treats "/" as a separator, so "/sta" was only replacing "sta" -> "//status".
        if len(parts) <= 1 and not text.endswith(" "):
            replace_len = len(parts[0])
            for name in _SLASH_COMMANDS:
                if name.startswith(cmd):
                    yield Completion(
                        name,
                        start_position=-replace_len,
                        display=name,
                        display_meta=self._get_command_description(name),
                    )
            # Also complete skill names (skip names that clash with built-in commands)
            if self._session:
                skills = getattr(self._session, "skills", [])
                builtin_names = set(_SLASH_COMMANDS)
                for skill in skills:
                    skill_cmd = f"/{skill.name}"
                    if skill_cmd in builtin_names:
                        continue
                    if skill_cmd.startswith(cmd):
                        yield Completion(
                            skill_cmd,
                            start_position=-replace_len,
                            display=skill_cmd,
                            display_meta=skill.description or skill.when_to_use or "skill",
                        )
            return

        # Complete arguments for specific commands
        if len(parts) >= 2 or text.endswith(" "):
            # /model <name> — complete model names
            if cmd == "/model":
                if self._session:
                    models = self._session.list_models()
                    for name in models:
                        if name.startswith(word_before_cursor):
                            yield Completion(
                                name,
                                start_position=-len(word_before_cursor),
                                display=name,
                            )
                return

            if cmd in {"/effort", "/ultra"}:
                for value in _SLASH_COMMANDS[cmd]:
                    if value.startswith(word_before_cursor):
                        yield Completion(
                            value,
                            start_position=-len(word_before_cursor),
                            display=value,
                        )
                return

            # /logs <name> — complete log names
            if cmd == "/logs":
                try:
                    from crabcode_search.background import list_background_logs
                    logs = list_background_logs(self._session.cwd if self._session else ".")
                except Exception:
                    logger.debug("Failed to load log names for completion", exc_info=True)
                    logs = {}
                for name in logs:
                    if name.startswith(word_before_cursor):
                        yield Completion(
                            name,
                            start_position=-len(word_before_cursor),
                            display=name,
                        )
                # Also complete flags
                for flag in _SLASH_COMMANDS.get("/logs", []):
                    if flag.startswith(word_before_cursor):
                        yield Completion(
                            flag,
                            start_position=-len(word_before_cursor),
                            display=flag,
                        )
                return

            # /resume <id> — complete session IDs
            if cmd == "/resume":
                from crabcode_core.session.storage import SessionStorage
                sessions = SessionStorage.list_sessions(self._session.cwd if self._session else ".")
                for s in sessions[:20]:
                    sid = s["session_id"]
                    if sid.startswith(word_before_cursor):
                        yield Completion(
                            sid,
                            start_position=-len(word_before_cursor),
                            display=sid[:12] + "…",
                            display_meta=s.get("preview", "")[:40],
                        )
                return

            if cmd in {"/agent", "/agent-log", "/agent-send", "/wait", "/cancel-agent"} and self._session:
                for snapshot in self._session.list_agents()[:20]:
                    sid = snapshot.agent_id
                    if sid.startswith(word_before_cursor):
                        yield Completion(
                            sid,
                            start_position=-len(word_before_cursor),
                            display=sid[:12] + "…",
                            display_meta=f"{snapshot.status} · {snapshot.title[:40]}",
                        )
                return

    def _get_command_description(self, cmd: str) -> str:
        descriptions = {
            "/help": "show help",
            "/goal": "set or manage the persistent task goal",
            "/plan": "switch to plan mode (read-only analysis)",
            "/agent": "switch to agent mode / show agent (<id>)",
            "/plan-status": "show current plan status",
            "/agents": "list managed agents",
            "/peers": "list messageable CrabCode sessions",
            "/tasks": "list/stop background agents and monitors",
            "/agent-log": "show an agent transcript",
            "/agent-send": "send input to an agent",
            "/wait": "wait for an agent",
            "/cancel-agent": "cancel an agent",
            "/team": "team lifecycle, messaging, and task-board management",
            "/schedule": "scheduled task lifecycle and execution history",
            "/status": "show session status",
            "/effort": "show/set reasoning effort",
            "/ultra": "toggle/set ultra mode",
            "/logs": "show background logs",
            "/model": "show/switch model",
            "/new": "start new session",
            "/compact": "compact conversation",
            "/clear": "clear history",
            "/sessions": "list sessions",
            "/recent": "list recent sessions (all projects)",
            "/search": "search sessions",
            "/archive": "archive a session",
            "/export": "export session (md/json)",
            "/stats": "usage statistics",
            "/checkpoint": "create checkpoint (with file snapshot)",
            "/checkpoints": "list checkpoints",
            "/rollback": "rollback conversation to checkpoint",
            "/revert": "revert files + conversation to checkpoint",
            "/undo": "undo last checkpoint (revert files + conversation)",
            "/resume": "resume session",
            "/image": "attach image to next message",
            "/exit": "exit CrabCode",
            "/quit": "exit CrabCode",
        }
        return descriptions.get(cmd, "")


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_VERBS = ["Thinking", "Reasoning", "Analyzing", "Processing", "Understanding"]

_COMPOSER_STYLE = Style.from_dict(
    {
        # prompt_toolkit gives bottom toolbars reverse video by default. This
        # toolbar is the lower edge of the frame, so retain the normal terminal
        # background.
        "bottom-toolbar": "noreverse",
    }
)


def _force_exit(code: int = 130) -> None:
    """Exit immediately without waiting for executor/native thread cleanup."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


_CTRL_C_EXIT_WINDOW_S = 5.0


def _composer_columns() -> int:
    """Return the live renderer width so frame edges follow terminal resizes."""
    app = get_app_or_none()
    if app is not None:
        return max(4, app.output.get_size().columns)
    try:
        return max(4, os.get_terminal_size(sys.stdout.fileno()).columns)
    except (OSError, ValueError):
        return 80


def _composer_frame_parts(
    left: str,
    label: str,
    right: str,
) -> tuple[str, str]:
    """Fit a label and return it with enough dashes to reach the right edge."""
    available = max(
        0,
        _composer_columns() - get_cwidth(left) - get_cwidth(right),
    )
    fitted: list[str] = []
    used = 0
    for char in label:
        char_width = get_cwidth(char)
        if used + char_width > available:
            break
        fitted.append(char)
        used += char_width
    return "".join(fitted), "─" * max(0, available - used)


def _composer_prompt(
    session: CoreSession,
    *,
    pending_images: bool = False,
) -> HTML:
    """Render the persistent, framed prompt used by idle and active turns."""
    mode = getattr(session, "_agent_mode", "agent")
    if mode == "plan":
        title = "CrabCode · plan"
    else:
        title = "CrabCode"
    attachment = " 📎" if pending_images else ""
    fitted_title, dashes = _composer_frame_parts("╭─ ", f"{title} ", "╮")
    return HTML(
        f"<ansicyan>╭─ {fitted_title}{dashes}╮</ansicyan>\n"
        f"<ansicyan>│</ansicyan><b> ❯{attachment} </b>"
    )


def _composer_toolbar(*, busy: bool = False, queued_count: int = 0) -> HTML:
    if busy and queued_count:
        hint = f" {queued_count} queued · Enter adds another · Ctrl+C interrupts "
    elif busy:
        hint = " Enter sends after the next tool call · Ctrl+C interrupts "
    else:
        hint = " Enter sends · Ctrl+D exits "
    fitted_hint, dashes = _composer_frame_parts("╰─", hint, "╯")
    return HTML(
        f"<ansicyan>╰─</ansicyan><gray>{fitted_hint}</gray>"
        f"<ansicyan>{dashes}╯</ansicyan>"
    )


def _configure_composer_layout(
    prompt_session: PromptSession[str],
    *,
    status_text: Callable[[], Any] | None = None,
    queued_text: Callable[[], Any] | None = None,
    has_queued_text: Callable[[], bool] | None = None,
) -> None:
    """Keep the composer and its status rows together at the terminal bottom."""
    root = prompt_session.layout.container
    if not isinstance(root, HSplit) or not root.children:
        return
    main = root.children[0]
    if not isinstance(main, FloatContainer) or not isinstance(main.content, HSplit):
        return

    # PromptSession lets its input container fill the area above a bottom
    # toolbar. Bottom-aligning the prompt within that area removes the large
    # gap that otherwise splits the frame in two.
    main.content.align = VerticalAlign.BOTTOM

    prefix_rows: list[Any] = []
    if status_text is not None:
        prefix_rows.append(
            Window(
                FormattedTextControl(status_text),
                height=Dimension.exact(1),
                dont_extend_height=True,
            )
        )
    if queued_text is not None and has_queued_text is not None:
        prefix_rows.append(
            ConditionalContainer(
                Window(
                    FormattedTextControl(queued_text),
                    height=Dimension.exact(1),
                    dont_extend_height=True,
                ),
                filter=Condition(has_queued_text),
            )
        )
    if prefix_rows:
        main.content.children = [*prefix_rows, *main.content.children]

    # The single-line input Window is also unbounded by default. Keep its
    # editing row at one line; completion menus remain floats and can still use
    # the free space above the composer.
    input_window = prompt_session.layout.current_window
    if isinstance(input_window, Window):
        input_window.height = Dimension.exact(1)

    # The stock bottom-toolbar Window has only a minimum height, so HSplit can
    # stretch it across all remaining rows and draw its text on the final one.
    # The frame edge is intrinsically a single line.
    toolbar = root.children[-1]
    if isinstance(toolbar, ConditionalContainer) and isinstance(
        toolbar.content,
        Window,
    ):
        toolbar.content.height = Dimension.exact(1)

    # Some embedded terminals clip the glyphs drawn on their physical last
    # row. Keep one reserved row below the frame so its lower border remains
    # fully visible instead of being cut in half by the terminal viewport.
    root.children.append(
        Window(
            height=Dimension.exact(1),
            dont_extend_height=True,
        )
    )

    main.floats.append(
        Float(
            right=0,
            ycursor=True,
            width=1,
            height=1,
            allow_cover_cursor=True,
            content=Window(
                FormattedTextControl(HTML("<ansicyan>│</ansicyan>")),
                width=1,
                height=1,
            ),
        )
    )


def _render_submitted_input(text: str, *, steering: bool = False) -> None:
    """Put submitted text into scrollback without leaving a stale frame."""
    prefix = "  ↳ " if steering else "  ❯ "
    line = Text(prefix, style="cyan")
    line.append(text)
    console.print(line)


class _PersistentComposer:
    """One long-lived input application shared by idle and working states."""

    def __init__(
        self,
        session: CoreSession,
        pending_images: list[dict[str, str]],
    ) -> None:
        self._session = session
        self._pending_images = pending_images
        self._events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._busy = False
        self._phase = ""
        self._activity_running = False
        self._activity_started_at = 0.0
        self._verb_index = 0
        self._notice = ""
        self._queued_messages: list[str] = []
        self._task: asyncio.Task[str] | None = None
        self._animation_task: asyncio.Task[None] | None = None

        bindings = KeyBindings()

        @bindings.add("c-c", eager=True)
        def _interrupt(event: Any) -> None:
            self._events.put_nowait(("interrupt", ""))

        @bindings.add("c-d", eager=True)
        def _eof(event: Any) -> None:
            self._events.put_nowait(("eof", ""))

        self.prompt_session: PromptSession[str] = PromptSession(
            message=lambda: _composer_prompt(
                self._session,
                pending_images=bool(self._pending_images),
            ),
            bottom_toolbar=lambda: _composer_toolbar(
                busy=self._busy,
                queued_count=len(self._queued_messages),
            ),
            history=InMemoryHistory(),
            completer=_CrabCodeCompleter(session),
            complete_while_typing=True,
            key_bindings=bindings,
            style=_COMPOSER_STYLE,
            erase_when_done=True,
        )
        _configure_composer_layout(
            self.prompt_session,
            status_text=self._status_text,
            queued_text=self._queued_text,
            has_queued_text=lambda: bool(self._queued_messages),
        )
        self.prompt_session.default_buffer.accept_handler = self._accept

    def _accept(self, buffer: Buffer) -> bool:
        text = buffer.text.strip()
        if text:
            self._notice = ""
            self._events.put_nowait(("submit", text))
        return False

    def _status_text(self) -> HTML:
        if self._busy:
            if self._activity_running:
                elapsed = max(
                    0.0,
                    time.monotonic() - self._activity_started_at,
                )
                frame = _SPINNER_FRAMES[
                    int(elapsed / 0.08) % len(_SPINNER_FRAMES)
                ]
                suffix = f" ({elapsed:.0f}s)" if elapsed >= 2 else ""
                return HTML(
                    f"<ansicyan>  {frame} {self._phase}…</ansicyan>"
                    f"<gray>{suffix}</gray>"
                )
            return HTML("<ansicyan>  ● Working</ansicyan>")
        if self._notice:
            return HTML(f"<gray>  ● {self._notice}</gray>")
        return HTML("<gray>  ● Ready</gray>")

    def _queued_text(self) -> list[tuple[str, str]]:
        latest = self._queued_messages[-1] if self._queued_messages else ""
        suffix = (
            f"  ({len(self._queued_messages)} queued)"
            if len(self._queued_messages) > 1
            else ""
        )
        return [
            ("fg:ansicyan", "  ↳ "),
            ("", latest),
            ("fg:ansigray", suffix),
        ]

    def _invalidate(self) -> None:
        if self.prompt_session.app.is_running:
            self.prompt_session.app.invalidate()

    async def _animate_status(self) -> None:
        try:
            while self._busy and self._activity_running:
                self._invalidate()
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            pass

    def _start_animation(self) -> None:
        if self._animation_task is None or self._animation_task.done():
            self._animation_task = asyncio.create_task(self._animate_status())

    def _stop_animation(self) -> None:
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        self._animation_task = None

    def start(self) -> None:
        self._task = asyncio.create_task(
            self.prompt_session.app.run_async(handle_sigint=False)
        )

    async def close(self) -> None:
        if self._task is None:
            return
        animation_task = self._animation_task
        self._stop_animation()
        if animation_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await animation_task
        if not self._task.done() and self.prompt_session.app.is_running:
            self.prompt_session.app.exit(result="")
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def next_event(self) -> tuple[str, str]:
        return await self._events.get()

    @property
    def has_notice(self) -> bool:
        return bool(self._notice)

    def set_busy(self, busy: bool) -> None:
        if busy and not self._busy:
            self._notice = ""
            self._verb_index = 0
        self._busy = busy
        if not busy:
            self._activity_running = False
            self._phase = ""
            self._stop_animation()
        self._invalidate()

    def start_activity(self, message: str | None = None) -> None:
        """Start one old-style spinner phase; keep its label stable."""
        if not self._busy or self._activity_running:
            return
        if message is None:
            message = _VERBS[self._verb_index % len(_VERBS)]
        # Legacy _Spinner advanced this counter for every phase start,
        # including named phases such as Generating and Running.
        self._verb_index += 1
        self._phase = message
        self._activity_started_at = time.monotonic()
        self._activity_running = True
        self._start_animation()
        self._invalidate()

    def update_activity(self, message: str) -> None:
        """Match the legacy spinner update without starting a stopped phase."""
        if not self._busy or not self._activity_running:
            return
        self._phase = message
        self._activity_started_at = time.monotonic()
        self._invalidate()

    def stop_activity(self) -> None:
        if not self._activity_running:
            return
        self._activity_running = False
        self._phase = ""
        self._stop_animation()
        self._invalidate()

    def set_notice(self, notice: str) -> None:
        self._busy = False
        self._activity_running = False
        self._phase = ""
        self._notice = notice
        self._stop_animation()
        self._invalidate()

    def add_guidance(self, text: str) -> None:
        self._queued_messages.append(text)
        self._invalidate()

    def mark_guidance_applied(self, count: int) -> list[str]:
        count = min(max(0, count), len(self._queued_messages))
        applied = self._queued_messages[:count]
        del self._queued_messages[:count]
        self._invalidate()
        return applied


class _CtrlCDoubleExit:
    """First Ctrl+C interrupts; a second within the window exits; otherwise reset."""

    __slots__ = ("_window_s", "_first_at")

    def __init__(self, window_s: float = _CTRL_C_EXIT_WINDOW_S) -> None:
        self._window_s = window_s
        self._first_at: float | None = None

    def should_exit_now(self) -> bool:
        """Record this Ctrl+C; return True if the user should exit (second tap in window)."""
        now = time.monotonic()
        if self._first_at is not None and (now - self._first_at) <= self._window_s:
            return True
        self._first_at = now
        return False

    def clear(self) -> None:
        self._first_at = None


# asyncio.run() installs a SIGINT handler: first Ctrl+C cancels the main task
# (CancelledError), the second raises KeyboardInterrupt. REPL must handle both.
_REPL_INTERRUPT_EXCS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    asyncio.CancelledError,
)


def _clear_sigint_cancel() -> None:
    """After handling first SIGINT under asyncio.run(), drop pending cancel so the REPL continues."""
    task = asyncio.current_task()
    if task is None:
        return
    uncancel = getattr(task, "uncancel", None)
    if uncancel is None:
        return
    while uncancel() > 0:
        pass


def _read_log_tail(path: Path, max_lines: int = 80) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(failed to read log: {exc})"
    lines = text.splitlines()
    if not lines:
        return "(log is empty)"
    if len(lines) > max_lines:
        lines = ["... (truncated)"] + lines[-max_lines:]
    return "\n".join(lines)


def _format_timestamp(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        logger.debug("Failed to format timestamp: %r", ts, exc_info=True)
        return "unknown"


def _parse_logs_args(arg: str) -> tuple[bool, bool, int, str | None, str | None]:
    follow = False
    clear = False
    tail = 80
    name: str | None = None
    error: str | None = None

    parts = arg.split() if arg else []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part in ("-f", "--follow"):
            follow = True
        elif part == "--clear":
            clear = True
        elif part == "--tail":
            i += 1
            if i >= len(parts):
                error = "--tail requires a number"
                break
            try:
                tail = max(1, int(parts[i]))
            except ValueError:
                error = "--tail requires an integer"
                break
        elif part.startswith("--tail="):
            value = part.split("=", 1)[1]
            try:
                tail = max(1, int(value))
            except ValueError:
                error = "--tail requires an integer"
                break
        elif part.startswith("-"):
            error = f"unknown option: {part}"
            break
        elif name is None:
            name = part
        else:
            error = f"unexpected argument: {part}"
            break
        i += 1

    return follow, clear, tail, name, error


async def _follow_log(path: Path, name: str) -> None:
    console.print(
        f"[dim]Following {name}. Press Ctrl+C to stop.[/]"
    )
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    await asyncio.sleep(0.5)
    except _REPL_INTERRUPT_EXCS:
        _clear_sigint_cancel()
        console.print("\n[dim]Stopped log follow.[/]")


class _Spinner:
    """Async terminal spinner with phase-aware messaging and elapsed timer."""

    def __init__(self, ansi_enabled: bool = True, visible: bool = True) -> None:
        self._ansi_enabled = ansi_enabled
        self._visible = visible
        self._task: asyncio.Task[None] | None = None
        self._message = ""
        self._running = False
        self._start_time = 0.0
        self._verb_index = 0
        self._last_line_len = 0

    def start(self, message: str | None = None) -> None:
        if self._running:
            return
        self._message = message or _VERBS[self._verb_index % len(_VERBS)]
        self._verb_index += 1
        self._running = True
        self._start_time = time.monotonic()
        if self._visible:
            self._task = asyncio.create_task(self._animate())

    def update(self, message: str) -> None:
        self._message = message
        self._start_time = time.monotonic()

    async def stop(self) -> float:
        """Stop spinner, wait for animation task, clear line, return elapsed seconds."""
        elapsed = time.monotonic() - self._start_time if self._running else 0.0
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._visible:
            self._clear_line()
        sys.stdout.flush()
        return elapsed

    @property
    def is_running(self) -> bool:
        return self._running

    async def _animate(self) -> None:
        try:
            idx = 0
            while self._running:
                frame = _SPINNER_FRAMES[idx % len(_SPINNER_FRAMES)]
                elapsed = time.monotonic() - self._start_time
                suffix = f" ({elapsed:.0f}s)" if elapsed >= 2 else ""
                line = f"{frame} {self._message}…{suffix}"
                if self._ansi_enabled:
                    sys.stdout.write(f"\r\033[K\033[2;36m{line}\033[0m")
                else:
                    pad = max(0, self._last_line_len - len(line))
                    sys.stdout.write(f"\r{line}{' ' * pad}")
                self._last_line_len = len(line)
                sys.stdout.flush()
                idx += 1
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            pass

    def _clear_line(self) -> None:
        if self._ansi_enabled:
            sys.stdout.write("\r\033[K")
            self._last_line_len = 0
            return
        if self._last_line_len:
            sys.stdout.write("\r" + (" " * self._last_line_len) + "\r")
            self._last_line_len = 0


def _tool_summary(name: str, inp: dict) -> str:
    """Return a human-readable one-liner for a tool call."""
    if name == "Bash":
        cmd = inp.get("command", "")
        lines = cmd.split("\n")
        if len(lines) > 3:
            return "\n".join(lines[:3]) + "\n…"
        return cmd
    if name in ("Write", "Edit", "Read"):
        return inp.get("file_path", inp.get("path", ""))
    if name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", ".")
        return f'"{pattern}" in {path}'
    if name == "Glob":
        return inp.get("pattern", inp.get("glob_pattern", ""))
    if name == "Agent":
        prompt = inp.get("prompt", "")
        return (prompt[:100] + "…") if len(prompt) > 100 else prompt
    if name == "Browser":
        action = inp.get("action", "")
        session_id = inp.get("session_id", "")
        selector = inp.get("selector", "")
        url = inp.get("url", "")
        text = inp.get("text", "")
        path = inp.get("path", "")
        lines = [f"action: {action}"]
        if session_id:
            lines.append(f"session_id: {session_id}")
        if url:
            lines.append(f"url: {url}")
        if selector:
            lines.append(f"selector: {selector}")
        if text:
            lines.append(f"text: {text[:120]}")
        if path:
            lines.append(f"path: {path}")
        return "\n".join(lines)
    if name == "AskUser":
        question = inp.get("question", "")
        options = inp.get("options", [])
        lines = [question]
        for i, opt in enumerate(options, 1):
            lines.append(f"  {i}. {opt}")
        return "\n".join(lines)
    if name == "PeerMessage":
        sender = inp.get("from_name", inp.get("from_session_id", "unknown"))
        sender_id = str(inp.get("from_session_id", ""))[:8]
        text = str(inp.get("text", ""))
        preview = text[:500] + ("…" if len(text) > 500 else "")
        return f"From {sender} · {sender_id}\n\n{preview}"
    import json
    raw = json.dumps(inp, ensure_ascii=False)
    return (raw[:200] + "…") if len(raw) > 200 else raw


def _debug_tool_payload_enabled() -> bool:
    return os.getenv("CRABCODE_DEBUG_TOOL_PAYLOAD", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _render_full_tool_payload(tool_name: str, tool_input: dict) -> None:
    payload = json.dumps(tool_input, ensure_ascii=False, indent=2)
    console.print(
        Panel(
            Text(payload, style="dim"),
            title=f"[bold yellow]Debug Payload: {tool_name}[/]",
            border_style="yellow",
            expand=False,
        )
    )


def _render_saved_partial_reply(text: str) -> None:
    """Echo assistant text that was persisted after interrupt (visible in scrollback)."""
    body = text.rstrip()
    if not body:
        return
    preview = body if len(body) <= 12000 else body[:12000] + "\n… (truncated in preview; full text is in context)"
    console.print(
        Panel(
            Text(preview, style="dim"),
            title="[dim]Assistant · partial (saved to context)[/]",
            border_style="dim",
            expand=False,
        )
    )


def _persist_partial_assistant_for_interrupt(session: CoreSession, raw: str) -> None:
    """Write streamed assistant text into the session and show it in the terminal."""
    console.print()
    if raw.strip():
        session.record_partial_assistant_output(raw)
        _render_saved_partial_reply(raw)
    else:
        console.print(
            "[dim](Interrupted before any assistant reply text; only your user message is in context.)[/]"
        )


def _render_tool_use(event: ToolUseEvent) -> None:
    """Render a compact tool use call."""
    summary = _tool_summary(event.tool_name, event.tool_input)
    agent_prefix = ""
    if event.agent_id:
        agent_prefix = f"[{event.agent_id[:8]}] "
    console.print(
        Panel(
            Text(summary, style="dim"),
            title=f"[bold cyan]{agent_prefix}{event.tool_name}[/]",
            border_style="cyan",
            expand=False,
        )
    )
    if _debug_tool_payload_enabled():
        _render_full_tool_payload(event.tool_name, event.tool_input)


def _truncate_display(display: str, max_lines: int = 50, max_chars: int = 50_000) -> str:
    """Truncate display text by line count and character count."""
    # Line-based truncation first
    lines = display.split("\n")
    if len(lines) > max_lines:
        display = "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines truncated)"
    # Character-based safety cap
    if len(display) > max_chars:
        display = display[:max_chars] + "\n... (truncated)"
    return display


def _render_tool_result(event: ToolResultEvent) -> None:
    """Render a tool result."""
    display = event.result_for_display or event.result

    if event.tool_name in ("Edit", "Write") and not event.is_error and "\n@@" in display:
        _render_diff_result(event.tool_name, display)
        return

    # Read display limits from settings if available
    ds = _display_settings
    max_lines = ds.get_max_lines(event.tool_name) if ds else 50
    max_chars = ds.max_chars if ds else 50_000
    display = _truncate_display(display, max_lines=max_lines, max_chars=max_chars)

    style = "red" if event.is_error else "green"
    title = f"{'Error' if event.is_error else 'Result'}: {event.tool_name}"
    if event.agent_id:
        title = f"[{event.agent_id[:8]}] {title}"
    console.print(
        Panel(
            Text(display, style="dim"),
            title=f"[bold {style}]{title}[/]",
            border_style=style,
            expand=False,
        )
    )


def _flush_agent_stream_line(active_agent_id: str | None) -> None:
    if active_agent_id is None:
        return
    sys.stdout.write("\n")
    sys.stdout.flush()


def _render_agent_text_chunk(
    event: AgentOutputEvent,
    active_agent_id: str | None,
) -> str | None:
    if event.stream != "text":
        return active_agent_id
    if active_agent_id != event.agent_id:
        _flush_agent_stream_line(active_agent_id)
        sys.stdout.write(f"[agent {event.agent_id[:8]}] ")
        active_agent_id = event.agent_id
    sys.stdout.write(safe_utf8_str(event.text))
    sys.stdout.flush()
    return active_agent_id


def _render_diff_result(tool_name: str, display: str) -> None:
    """Render a diff result with colored +/- lines."""
    lines = display.split("\n")
    header = lines[0] if lines else ""

    diff_parts: list[Text] = []
    for line in lines[1:]:
        if line.startswith("+++") or line.startswith("---"):
            diff_parts.append(Text(line, style="bold dim"))
        elif line.startswith("@@"):
            diff_parts.append(Text(line, style="cyan"))
        elif line.startswith("+"):
            diff_parts.append(Text(line, style="green"))
        elif line.startswith("-"):
            diff_parts.append(Text(line, style="red"))
        elif line.startswith("... (diff truncated)"):
            diff_parts.append(Text(line, style="yellow dim"))
        else:
            diff_parts.append(Text(line, style="dim"))

    body = Text("\n").join(diff_parts) if diff_parts else Text("(no diff)", style="dim")

    if len(display) > 5000:
        body = Text(display[:5000] + "\n... (truncated)", style="dim")

    console.print(
        Panel(
            body,
            title=f"[bold green]{tool_name}: {header}[/]",
            border_style="green",
            expand=False,
        )
    )


def _render_session_history(messages: list[Message], max_messages: int = 50) -> None:
    """Render a condensed view of conversation history after resuming a session."""
    if not messages:
        return

    displayed = messages[-max_messages:]
    if len(messages) > max_messages:
        console.print(
            f"  [dim italic]... {len(messages) - max_messages} earlier messages omitted ...[/]\n"
        )

    for msg in displayed:
        if msg.role == MessageRole.USER:
            if (
                getattr(msg, "source_tool_assistant_uuid", None)
                or getattr(msg, "origin", None) == "task-notification"
            ):
                continue
            text = msg.text_content.strip()
            if not text or text.startswith("<system-reminder>"):
                continue
            preview = text[:200] + ("…" if len(text) > 200 else "")
            console.print(f"[bold cyan]❯[/] {preview}")

        elif msg.role == MessageRole.ASSISTANT:
            text = msg.text_content.strip()
            tool_blocks = msg.tool_use_blocks

            if text:
                preview = text[:300] + ("…" if len(text) > 300 else "")
                console.print(f"[dim]{preview}[/]")

            if tool_blocks:
                names = [b.name for b in tool_blocks]
                console.print(f"  [dim cyan]⚡ {', '.join(names)}[/]")

            if not text and not tool_blocks:
                continue

    console.print()


async def _prompt_permission(
    event: PermissionRequestEvent,
    session: CoreSession,
    batch_state: dict | None = None,
) -> None:
    """Prompt the user for tool permission and push response to session."""
    is_peer_message = event.request_kind == "peer_message"
    # If a previous request in this batch was denied, auto-deny silently
    if not is_peer_message and batch_state and batch_state.get("denied"):
        await session.respond_permission(
            PermissionResponseEvent(
                tool_use_id=event.tool_use_id, allowed=False, agent_id=event.agent_id
            )
        )
        console.print(
            f"  [dim red]✗ {event.tool_name} auto-denied (batch)[/]"
        )
        return

    summary = _tool_summary(event.tool_name, event.tool_input)
    if event.reason:
        summary = f"{summary}\n\nReason: {event.reason}"
    console.print(
        Panel(
            Text(summary, style="dim"),
            title=(
                "[bold yellow]⚠ Cross-session message[/]"
                if is_peer_message
                else f"[bold yellow]⚠ {event.tool_name}{f' [{event.agent_id[:8]}]' if event.agent_id else ''}[/]"
            ),
            border_style="yellow",
            expand=False,
        )
    )

    perm_prompt_session: PromptSession[str] = PromptSession()
    while True:
        try:
            choice = await perm_prompt_session.prompt_async(
                HTML(
                    (
                        "  Receive this message? "
                        "(y)es / (n)o / (a)lways receive from this session: "
                        if is_peer_message
                        else f"  Allow <b>{event.tool_name}</b>? "
                        "(y)es / (n)o / (a)lways allow / (f)eedback: "
                    )
                )
            )
            choice = choice.strip().lower()
        except (EOFError, KeyboardInterrupt):
            if not is_peer_message and batch_state is not None:
                batch_state["denied"] = True
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=False, agent_id=event.agent_id
                )
            )
            return

        if choice in ("y", "yes", ""):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=True, agent_id=event.agent_id
                )
            )
            return
        elif choice in ("n", "no"):
            if not is_peer_message and batch_state is not None:
                batch_state["denied"] = True
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=False, agent_id=event.agent_id
                )
            )
            return
        elif choice in ("a", "always"):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id,
                    allowed=True,
                    always_allow=True,
                    agent_id=event.agent_id,
                )
            )
            return
        elif choice in ("f", "feedback"):
            try:
                feedback_session: PromptSession[str] = PromptSession()
                feedback = await feedback_session.prompt_async(
                    HTML("  Feedback (what to do instead): ")
                )
                feedback = feedback.strip()
            except (EOFError, KeyboardInterrupt):
                feedback = ""
            if not is_peer_message and batch_state is not None:
                batch_state["denied"] = True
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id,
                    allowed=False,
                    agent_id=event.agent_id,
                    feedback=feedback or None,
                )
            )
            return
        else:
            console.print("  [dim]Please enter y, n, a, or f[/]")


async def _interactive_select(question: str, options: list[str], multiple: bool = False) -> list[str]:
    """Interactive single/multi select using keyboard navigation.

    Returns a list of selected option strings.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style

    current = 0
    selected: set[int] = set() if multiple else set()

    def get_text() -> FormattedText:
        fragments: list[tuple[str, str]] = []
        # Question
        fragments.append(("class:question", f"  {safe_utf8_str(question)}\n\n"))

        for i, opt in enumerate(options):
            if multiple:
                checked = "◉" if i in selected else "○"
            else:
                checked = "●" if i == current and not selected else "○"
                if i in selected:
                    checked = "◉"

            if i == current:
                prefix = f"  ❯ {checked} "
                style = "class:selected"
            else:
                prefix = f"    {checked} "
                style = "class:option"

            fragments.append((style, f"{prefix}{safe_utf8_str(opt)}\n"))

        fragments.append(("", "\n"))
        if multiple:
            hint = "  ↑↓ navigate · space select · enter confirm · esc cancel"
        else:
            hint = "  ↑↓ navigate · enter select · esc cancel"
        fragments.append(("class:hint", hint))
        return fragments

    class SelectControl(FormattedTextControl):
        def __init__(self) -> None:
            super().__init__(text=get_text, focusable=True)

        def move_cursor_down(self) -> None:
            nonlocal current
            current = min(current + 1, len(options) - 1)

        def move_cursor_up(self) -> None:
            nonlocal current
            current = max(current - 1, 0)

    control = SelectControl()

    kb = KeyBindings()

    @kb.add("up")
    def _up(event: Any) -> None:
        control.move_cursor_up()

    @kb.add("down")
    def _down(event: Any) -> None:
        control.move_cursor_down()

    @kb.add("j")
    def _j(event: Any) -> None:
        control.move_cursor_up()

    @kb.add("k")
    def _k(event: Any) -> None:
        control.move_cursor_down()

    if multiple:
        @kb.add("space")
        def _toggle(event: Any) -> None:
            if current in selected:
                selected.discard(current)
            else:
                selected.add(current)

    @kb.add("enter")
    def _confirm(event: Any) -> None:
        if multiple:
            if not selected:
                selected.add(current)
        else:
            selected.clear()
            selected.add(current)
        event.app.exit(result=list(selected))

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    style_dict = {
        "question": "bold ansicyan",
        "selected": "bold",
        "option": "",
        # prompt_toolkit has no "dim"; use subdued palette color for hints.
        "hint": "ansibrightblack",
    }

    layout = Layout(HSplit([Window(content=control, height=len(options) + 4)]))

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        style=Style.from_dict(style_dict),
    )

    result = await app.run_async()

    if result is None:
        return []

    return [options[i] for i in sorted(result)]


async def _prompt_choice(
    event: ChoiceRequestEvent,
    session: CoreSession,
) -> None:
    """Present an interactive choice to the user and push response to session."""
    loop = asyncio.get_event_loop()

    if _ANSI_ENABLED and sys.stdin.isatty():
        try:
            selected = await _interactive_select(
                f"{event.question}{f' [{event.agent_id[:8]}]' if event.agent_id else ''}",
                event.options,
                event.multiple,
            )
        except (EOFError, KeyboardInterrupt):
            selected = []

        if not selected:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=[],
                    cancelled=True,
                    agent_id=event.agent_id,
                )
            )
        else:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=selected,
                    agent_id=event.agent_id,
                )
            )
    else:
        # Fallback for non-interactive terminals: numbered text selection
        suffix = f" [agent {event.agent_id[:8]}]" if event.agent_id else ""
        console.print(f"\n  [bold cyan]? {event.question}{suffix}[/]")
        for i, opt in enumerate(event.options, 1):
            console.print(f"    [dim]{i}.[/] {opt}")

        try:
            default = "1"
            raw = await loop.run_in_executor(
                None,
                lambda: input(f"  Enter choice [{default}]: ").strip() or default,
            )
        except (EOFError, KeyboardInterrupt):
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=[],
                    cancelled=True,
                    agent_id=event.agent_id,
                )
            )
            return

        if event.multiple:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
            indices = [i for i in indices if 0 <= i < len(event.options)]
            selected = [event.options[i] for i in indices] if indices else []
        else:
            try:
                idx = int(raw) - 1
                selected = [event.options[idx]] if 0 <= idx < len(event.options) else []
            except (ValueError, IndexError):
                selected = []

        if not selected:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=[],
                    cancelled=True,
                    agent_id=event.agent_id,
                )
            )
        else:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=selected,
                    agent_id=event.agent_id,
                )
            )


async def _consume_background_events(
    session: CoreSession,
) -> None:
    """Render events from automatic continuations while the REPL is idle."""
    text_parts: list[str] = []
    announced = False
    permission_batch: dict[str, bool] = {"denied": False}

    async def render(action: Any) -> None:
        async with in_terminal():
            action()

    async def announce() -> None:
        nonlocal announced
        if announced:
            return
        await render(
            lambda: console.print(
                "\n  [dim cyan]↻ Background agent continuation[/]"
            )
        )
        announced = True

    async def flush_text() -> None:
        if not text_parts:
            return
        body = safe_utf8_str("".join(text_parts)).rstrip()
        text_parts.clear()
        if body:
            await render(lambda: console.print(Text(body)))

    while True:
        event = await session.next_background_event()

        if isinstance(event, StreamTextEvent):
            await announce()
            text_parts.append(event.text)
            continue
        if isinstance(event, StreamModeEvent):
            if event.mode == "compacting":
                await announce()
                await render(
                    lambda: console.print("  [dim italic]Compacting conversation…[/]")
                )
            continue
        if isinstance(event, (ThinkingEvent, AgentOutputEvent)):
            continue

        if isinstance(event, AgentStateEvent):
            await announce()
            style = {
                "queued": "dim",
                "running": "cyan",
                "completed": "green",
                "failed": "red",
                "cancelled": "yellow",
                "stopped": "yellow",
            }.get(event.status, "dim")
            await render(
                lambda: console.print(
                    f"  [{style}]agent {event.agent_id[:8]} · "
                    f"{event.status} · {event.title}[/]"
                )
            )
            continue

        await announce()
        await flush_text()

        if isinstance(event, ToolUseEvent):
            await render(lambda: _render_tool_use(event))
        elif isinstance(event, ToolResultEvent):
            await render(lambda: _render_tool_result(event))
        elif isinstance(event, PermissionRequestEvent):
            async with in_terminal():
                await _prompt_permission(event, session, permission_batch)
        elif isinstance(event, ChoiceRequestEvent):
            async with in_terminal():
                await _prompt_choice(event, session)
        elif isinstance(event, CompactEvent):
            await render(
                lambda: console.print(
                    f"[dim italic]Conversation compacted: {event.summary}[/]"
                )
            )
        elif isinstance(event, ErrorEvent):
            await render(
                lambda: console.print(
                    f"[bold red]Error: {safe_utf8_str(event.message)}[/]"
                )
            )
        elif isinstance(event, ModeChangeEvent):
            session.switch_mode(event.mode)
            await render(
                lambda: console.print(
                    f"  [dim]Background continuation switched to {event.mode} mode[/]"
                )
            )
        elif isinstance(event, PlanReadyEvent):
            session.set_plan(event.plan)
            await render(lambda: console.print("  [bold blue]Background plan ready[/]"))
        elif isinstance(event, PeerMessageEvent):
            await render(
                lambda: console.print(
                    f"  [dim cyan][peer:{event.from_name}] {event.text[:200]}[/]"
                )
            )
        elif isinstance(event, TeamMessageEvent):
            await render(
                lambda: console.print(
                    f"  [dim magenta][team:{event.team_id[:8]}] "
                    f"{event.from_agent[:8]} → {event.to_agent[:8]}: "
                    f"{event.text[:100]}[/]"
                )
            )
        elif isinstance(event, TeamStateEvent):
            await render(
                lambda: console.print(
                    f"  [dim magenta][team:{event.team_id[:8]}] "
                    f"{event.agent_id[:8]} {event.old_state} → {event.new_state}[/]"
                )
            )
        elif isinstance(event, TaskUpdateEvent):
            await render(
                lambda: console.print(
                    f"  [dim magenta][team:{event.team_id[:8]}] "
                    f"task {event.task_id[:8]} {event.status}[/]"
                )
            )
        elif isinstance(event, ScheduleRunEvent):
            status_style = "green" if event.status == "success" else "red"
            detail = event.error_message or event.result_summary or event.status
            await render(
                lambda: console.print(
                    f"  [dim cyan][schedule:{event.job_id[:8]}] "
                    f"[{status_style}]{event.status}[/] {safe_utf8_str(detail)}[/]"
                )
            )
        elif isinstance(event, TurnCompleteEvent):
            await render(lambda: _render_context_usage(event))
            permission_batch["denied"] = False
            announced = False


async def _stream_agent_until_done(
    session: CoreSession,
    target_agent_id: str,
) -> None:
    active_stream_agent: str | None = None
    while True:
        event = await session._agent_event_queue.get()  # type: ignore[attr-defined]

        if isinstance(event, AgentOutputEvent):
            active_stream_agent = _render_agent_text_chunk(event, active_stream_agent)
            if event.stream == "tool_use" and event.tool_name:
                _flush_agent_stream_line(active_stream_agent)
                active_stream_agent = None
                console.print(
                    f"  [dim cyan]↳ agent {event.agent_id[:8]} using {event.tool_name}[/]"
                )
            continue

        if isinstance(event, AgentStateEvent):
            _flush_agent_stream_line(active_stream_agent)
            active_stream_agent = None
            style = {
                "queued": "dim",
                "running": "cyan",
                "completed": "green",
                "failed": "red",
                "cancelled": "yellow",
                "stopped": "yellow",
            }.get(event.status, "dim")
            console.print(
                f"  [{style}]agent {event.agent_id[:8]} · {event.status} · {event.title}[/]"
            )
            if event.agent_id == target_agent_id and event.status in {
                "completed",
                "failed",
                "stopped",
                "cancelled",
            }:
                break
            continue

        _flush_agent_stream_line(active_stream_agent)
        active_stream_agent = None

        if isinstance(event, ToolUseEvent):
            _render_tool_use(event)
        elif isinstance(event, ToolResultEvent):
            if event.tool_name == "AskUser":
                if event.is_error:
                    console.print("  [dim yellow]↳ Selection cancelled[/]")
                else:
                    console.print(f"  [dim green]↳ {safe_utf8_str(event.result)}[/]")
            else:
                _render_tool_result(event)
        elif isinstance(event, PermissionRequestEvent):
            await _prompt_permission(event, session)
        elif isinstance(event, ChoiceRequestEvent):
            await _prompt_choice(event, session)
        elif isinstance(event, ErrorEvent):
            console.print(f"\n[bold red]Error: {safe_utf8_str(event.message)}[/]")

    _flush_agent_stream_line(active_stream_agent)


async def _run_plan_executor_with_runtime_events(
    session: CoreSession,
    plan: object,
) -> None:
    from crabcode_core.plan.executor import PlanExecutor

    merged_events: asyncio.Queue[object] = asyncio.Queue()
    done_sentinel = object()

    async def _produce_plan_events() -> None:
        executor = PlanExecutor(
            plan=plan,
            spawn_fn=session.spawn_agent,
            wait_fn=session.wait_agent,
            cancel_fn=session.cancel_agent,
        )
        try:
            async for plan_event in executor.execute():
                await merged_events.put(plan_event)
        finally:
            await merged_events.put(done_sentinel)

    async def _forward_agent_events() -> None:
        while True:
            event = await session._agent_event_queue.get()  # type: ignore[attr-defined]
            await merged_events.put(event)

    producer = asyncio.create_task(_produce_plan_events())
    forwarder = asyncio.create_task(_forward_agent_events())
    active_stream_agent: str | None = None
    batch_state: dict = {"denied": False}

    try:
        while True:
            event = await merged_events.get()
            if event is done_sentinel:
                break

            if isinstance(event, StreamTextEvent):
                _flush_agent_stream_line(active_stream_agent)
                active_stream_agent = None
                console.print(f"  {safe_utf8_str(event.text)}", end="")
                continue

            if isinstance(event, AgentOutputEvent):
                active_stream_agent = _render_agent_text_chunk(event, active_stream_agent)
                if event.stream == "tool_use" and event.tool_name:
                    _flush_agent_stream_line(active_stream_agent)
                    active_stream_agent = None
                    console.print(
                        f"  [dim cyan]↳ agent {event.agent_id[:8]} using {event.tool_name}[/]"
                    )
                continue

            _flush_agent_stream_line(active_stream_agent)
            active_stream_agent = None

            if isinstance(event, AgentStateEvent):
                style = {
                    "queued": "dim",
                    "running": "cyan",
                    "completed": "green",
                    "failed": "red",
                    "cancelled": "yellow",
                    "stopped": "yellow",
                }.get(event.status, "dim")
                console.print(
                    f"  [{style}]agent {event.agent_id[:8]} · {event.status} · {event.title}[/]"
                )
            elif isinstance(event, StreamModeEvent):
                if event.mode == "tool-input":
                    batch_state["denied"] = False
            elif isinstance(event, TurnCompleteEvent):
                batch_state["denied"] = False
            elif isinstance(event, ToolUseEvent):
                _render_tool_use(event)
            elif isinstance(event, ToolResultEvent):
                if event.tool_name == "AskUser":
                    if event.is_error:
                        console.print("  [dim yellow]↳ Selection cancelled[/]")
                    else:
                        console.print(f"  [dim green]↳ {safe_utf8_str(event.result)}[/]")
                else:
                    _render_tool_result(event)
            elif isinstance(event, PermissionRequestEvent):
                await _prompt_permission(event, session, batch_state)
            elif isinstance(event, ChoiceRequestEvent):
                await _prompt_choice(event, session)
            elif isinstance(event, ErrorEvent):
                console.print(f"  [bold red]{safe_utf8_str(event.message)}[/]")
    finally:
        forwarder.cancel()
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forwarder
        with contextlib.suppress(asyncio.CancelledError):
            await producer
        _flush_agent_stream_line(active_stream_agent)


async def run_repl(
    settings: CrabCodeSettings | None = None,
    cwd: str = ".",
    resume_session_id: str | None = None,
) -> None:
    """Run the interactive REPL."""
    if resume_session_id:
        # Defensive resolution for callers that invoke run_repl directly
        # (without going through the Typer entry point).  Project settings,
        # tools, and LSP must be initialized against the resumed session's cwd.
        from crabcode_core.session.storage import SessionStorage

        resolved_storage = SessionStorage.from_session_id(resume_session_id)
        if resolved_storage is not None:
            cwd = resolved_storage.cwd

    print_banner(console)
    console.print(f"  cwd: {cwd}", style="dim")
    if settings:
        active_cfg = settings.get_api_config()
        provider = active_cfg.provider
        model = active_cfg.model
        model_label = f"[bold cyan]{settings.default_model}[/] → " if settings.default_model else ""
    else:
        provider = model = None
        model_label = ""
    provider_str = provider or "[yellow]not set[/]"
    model_str = model or "[yellow]not set[/]"
    console.print(f"  provider: {provider_str}  model: {model_label}{model_str}", style="dim")
    if not model:
        console.print(
            "  [bold yellow]Warning:[/] no model configured. "
            "Set [bold]api.model[/] or [bold]models[/] in ~/.crabcode/settings.json or use [bold]-m[/] flag.",
            style="dim",
        )
    console.print(
        "  Type /help for commands. "
        "You can send guidance while the agent is working. "
        f"Ctrl+C interrupts; press again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit. "
        "Ctrl+D exits.",
        style="dim",
    )

    if settings and (
        settings.permissions.run_everything
        or settings.permissions.default_mode in {"run_everything", "bypassPermissions"}
    ):
        console.print()
        console.print(
            "  [bold yellow]⚠ WARNING: run_everything mode is enabled.[/] "
            "All tool calls will execute automatically without asking for permission.",
            style="yellow",
        )

    console.print()

    session = CoreSession(cwd=cwd, settings=settings)
    background_consumer: asyncio.Task[None] | None = None
    composer: _PersistentComposer | None = None
    stdout_patch: Any | None = None

    # Pending image attachments for the next user message
    pending_images: list[dict[str, str]] = []

    global _display_settings
    _display_settings = settings.display if settings else None

    _progress_line_len = 0

    def _on_tool_event(tool_name: str, event_type: str, data: dict) -> None:
        nonlocal _progress_line_len
        if event_type == "progress":
            msg = data.get("message", "")
            pct = data.get("percent")
            bar = ""
            if pct is not None:
                filled = int(pct * 20)
                bar = f" [{'█' * filled}{'░' * (20 - filled)}] {int(pct * 100)}%"
            line = f"  {tool_name}: {msg}{bar}"
            # Avoid ANSI clear-line control sequences; pad with spaces instead.
            pad = max(0, _progress_line_len - len(line))
            sys.stdout.write(f"\r{line}{' ' * pad}")
            _progress_line_len = len(line)
            sys.stdout.flush()
        elif event_type == "ready":
            if _progress_line_len:
                sys.stdout.write("\r" + (" " * _progress_line_len) + "\r")
                sys.stdout.flush()
                _progress_line_len = 0
            console.print(f"  [green]✓[/] {tool_name}: {data.get('message', 'ready')}")

    session.on_tool_event = _on_tool_event

    try:
        if resume_session_id:
            await session.initialize()
            ok = await session.resume(resume_session_id)
            if ok:
                console.print(
                    f"  [dim]Resumed session [bold]{resume_session_id[:8]}…[/bold] "
                    f"({len(session.messages)} messages)[/]"
                )
                console.print()
                _render_session_history(session.messages)
            else:
                console.print(
                    f"  [bold yellow]Warning:[/] session {resume_session_id[:8]}… not found, starting fresh.",
                    style="dim",
                )
                console.print()
        else:
            pass

        composer = _PersistentComposer(session, pending_images)
        stdout_patch = patch_stdout(raw=True)
        stdout_patch.__enter__()
        composer.start()
        background_consumer = asyncio.create_task(
            _consume_background_events(session)
        )

        async def _force_exit_cleanly() -> None:
            """Restore terminal modes before the REPL's immediate exit path."""
            await composer.close()
            if stdout_patch is not None:
                stdout_patch.__exit__(None, None, None)
            try:
                await session.close()
            except Exception:
                logger.debug("Failed to close session during forced exit", exc_info=True)
            _force_exit()

        ctrl_c_exit = _CtrlCDoubleExit()
        input_state: dict[str, Any] = {
            "shutdown": False,
            "deferred": [],
            "interrupt_requested": False,
            "force_exit": False,
        }

        while True:
            deferred_inputs = input_state.get("deferred", [])
            if deferred_inputs:
                user_input = deferred_inputs.pop(0)
            else:
                while True:
                    event_kind, event_text = await composer.next_event()
                    if event_kind == "submit":
                        user_input = event_text
                        break
                    if event_kind == "eof":
                        input_state["shutdown"] = True
                        break
                    if event_kind == "interrupt":
                        if ctrl_c_exit.should_exit_now():
                            console.print("\nGoodbye!", style="dim")
                            try:
                                await session.interrupt()
                            except Exception:
                                logger.debug(
                                    "Failed to interrupt session during forced exit",
                                    exc_info=True,
                                )
                            await _force_exit_cleanly()
                        try:
                            await session.interrupt()
                        except Exception:
                            logger.debug(
                                "Failed to interrupt session after Ctrl+C",
                                exc_info=True,
                            )
                        composer.set_notice(
                            f"Interrupted · Ctrl+C again within "
                            f"{_CTRL_C_EXIT_WINDOW_S:.0f}s to exit"
                        )

                if input_state.get("shutdown"):
                    console.print("\nGoodbye!", style="dim")
                    break

            user_input = user_input.strip()
            if not user_input:
                continue

            _render_submitted_input(user_input)

            ctrl_c_exit.clear()

            if user_input.startswith("/"):
                async with in_terminal():
                    result = await _handle_command(
                        user_input,
                        session,
                        settings,
                        pending_images,
                    )
                if result is False:
                    break
                if isinstance(result, str):
                    user_input = result
                else:
                    continue

            if user_input.startswith("! "):
                cmd = user_input[2:]
                import subprocess
                async with in_terminal():
                    subprocess.run(cmd, shell=True, cwd=cwd, capture_output=False)
                continue

            streamed_text = ""
            streamed_text_for_context = ""
            # The persistent composer itself shows the working state. Avoid a
            # rapidly redrawn spinner competing with prompt_toolkit for the
            # terminal's bottom row.
            spinner = _Spinner(ansi_enabled=_ANSI_ENABLED, visible=False)
            thinking_start: float = 0.0
            is_thinking = False

            def _finish_stream_line() -> None:
                nonlocal streamed_text
                if streamed_text:
                    if not streamed_text.endswith("\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
                    streamed_text = ""

            async def _stop_spinner_with_thinking() -> None:
                nonlocal is_thinking
                composer.stop_activity()
                if not spinner.is_running:
                    return
                await spinner.stop()
                if is_thinking and thinking_start:
                    duration = time.monotonic() - thinking_start
                    if duration >= 1:
                        console.print(
                            f"  [dim italic]∴ Thought for {max(1, round(duration))}s[/]"
                        )
                is_thinking = False

            plan_pending = False
            input_state["interrupt_requested"] = False
            input_state["force_exit"] = False
            composer.set_busy(True)

            async def _consume_turn_input() -> None:
                while True:
                    event_kind, event_text = await composer.next_event()
                    if event_kind == "submit":
                        text = event_text.strip()
                        if not text:
                            continue
                        if await session.steer_message(text):
                            composer.add_guidance(text)
                            continue
                        input_state.setdefault("deferred", []).append(text)
                        return

                    if event_kind == "eof":
                        input_state["shutdown"] = True
                        await session.interrupt()
                        return

                    if event_kind == "interrupt":
                        if ctrl_c_exit.should_exit_now():
                            input_state["force_exit"] = True
                        else:
                            input_state["interrupt_requested"] = True
                        composer.stop_activity()
                        composer.start_activity("Interrupting")
                        await session.interrupt()

            steering_task = asyncio.create_task(_consume_turn_input())
            try:
                send_images = pending_images.copy() if pending_images else None
                pending_images.clear()
                batch_state: dict = {"denied": False}
                async for event in session.send_message(user_input, images=send_images):
                    if isinstance(event, StreamModeEvent):
                        if event.mode == "requesting":
                            spinner.start()
                            composer.start_activity()
                            is_thinking = False
                            thinking_start = 0.0
                        elif event.mode == "compacting":
                            spinner.start("Compacting")
                            composer.start_activity("Compacting")
                            is_thinking = False
                            thinking_start = 0.0
                        elif event.mode == "thinking":
                            thinking_start = time.monotonic()
                            is_thinking = True
                            spinner.update("Thinking")
                            composer.update_activity("Thinking")
                        elif event.mode == "responding":
                            await _stop_spinner_with_thinking()
                        elif event.mode == "tool-input":
                            batch_state["denied"] = False
                            await _stop_spinner_with_thinking()
                            _finish_stream_line()
                            spinner.start("Generating")
                            composer.start_activity("Generating")
                        elif event.mode == "tool-running":
                            await _stop_spinner_with_thinking()
                            _finish_stream_line()
                            spinner.start("Running")
                            composer.start_activity("Running")

                    elif isinstance(event, ThinkingEvent):
                        pass

                    elif isinstance(event, StreamTextEvent):
                        await _stop_spinner_with_thinking()
                        chunk = safe_utf8_str(event.text)
                        sys.stdout.write(chunk)
                        # StdoutProxy keeps a partial line until it receives a
                        # newline. Flushing every token makes a persistent
                        # prompt repaint those fragments at the same cursor
                        # position, which visually drops the beginning of the
                        # response. Complete lines are emitted immediately;
                        # _finish_stream_line commits the final partial line.
                        streamed_text += chunk
                        streamed_text_for_context += event.text

                    elif isinstance(event, ToolUseEvent):
                        if event.tool_name == "AskUser":
                            pass
                        else:
                            await _stop_spinner_with_thinking()
                            _finish_stream_line()
                            _render_tool_use(event)

                    elif isinstance(event, AgentStateEvent):
                        _finish_stream_line()
                        style = {
                            "queued": "dim",
                            "running": "cyan",
                            "completed": "green",
                            "failed": "red",
                            "cancelled": "yellow",
                            "stopped": "yellow",
                        }.get(event.status, "dim")
                        console.print(
                            f"  [{style}]agent {event.agent_id[:8]} · {event.status} · {event.title}[/]"
                        )

                    elif isinstance(event, AgentOutputEvent):
                        if event.stream == "tool_use" and event.tool_name:
                            _finish_stream_line()
                            console.print(
                                f"  [dim cyan]↳ agent {event.agent_id[:8]} using {event.tool_name}[/]"
                            )

                    elif isinstance(event, PermissionRequestEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        async with in_terminal():
                            await _prompt_permission(event, session, batch_state)

                    elif isinstance(event, ChoiceRequestEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        async with in_terminal():
                            await _prompt_choice(event, session)

                    elif isinstance(event, ToolResultEvent):
                        _finish_stream_line()
                        if event.tool_name == "AskUser":
                            await _stop_spinner_with_thinking()
                            if event.is_error:
                                console.print("  [dim yellow]↳ Selection cancelled[/]")
                            else:
                                console.print(f"  [dim green]↳ {safe_utf8_str(event.result)}[/]")
                        else:
                            await _stop_spinner_with_thinking()
                            _render_tool_result(event)

                    elif isinstance(event, CompactEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        console.print(
                            f"\n[dim italic]Conversation compacted: {event.summary}[/]"
                        )

                    elif isinstance(event, ErrorEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        console.print(
                            f"\n[bold red]Error: {safe_utf8_str(event.message)}[/]"
                        )
                        if not event.recoverable:
                            break

                    elif isinstance(event, ModeChangeEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        session.switch_mode(event.mode)
                        if event.mode == "plan":
                            console.print(
                                "\n  [bold blue]Switched to plan mode[/] — read-only, agent will only plan"
                            )
                        else:
                            console.print(
                                "\n  [bold green]Switched to agent mode[/] — full tool access"
                            )

                    elif isinstance(event, TeamMessageEvent):
                        _finish_stream_line()
                        console.print(
                            f"  [dim magenta][team:{event.team_id[:8]}] "
                            f"{event.from_agent[:8]} → {event.to_agent[:8]}: "
                            f"{event.text[:100]}[/]"
                        )

                    elif isinstance(event, PeerMessageEvent):
                        _finish_stream_line()
                        console.print(
                            f"  [dim cyan][peer:{event.from_name}] {event.text[:200]}[/]"
                        )

                    elif isinstance(event, TeamStateEvent):
                        _finish_stream_line()
                        console.print(
                            f"  [dim magenta][team:{event.team_id[:8]}] "
                            f"{event.agent_id[:8]} {event.old_state} → {event.new_state}[/]"
                        )

                    elif isinstance(event, TaskUpdateEvent):
                        _finish_stream_line()
                        console.print(
                            f"  [dim magenta][team:{event.team_id[:8]}] "
                            f"task {event.task_id[:8]} {event.status}"
                            f"{f' by {event.assignee[:8]}' if event.assignee else ''}[/]"
                        )

                    elif isinstance(event, ScheduleRunEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        status_style = "green" if event.status == "success" else "red"
                        detail = event.error_message or event.result_summary or event.status
                        console.print(
                            f"\n  [dim cyan][schedule:{event.job_id[:8]}] "
                            f"[{status_style}]{event.status}[/] {safe_utf8_str(detail)}[/]"
                        )

                    elif isinstance(event, PlanReadyEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        session.set_plan(event.plan)
                        from crabcode_core.plan.types import ExecutionPlan
                        plan = ExecutionPlan.from_dict(event.plan)
                        console.print(f"\n  [bold]Plan received:[/] {plan.title}")
                        console.print(f"  [dim]{plan.summary}[/]")
                        console.print(Panel(
                            plan.render(),
                            title="[bold]Execution Plan[/]",
                            border_style="blue",
                            expand=False,
                        ))
                        # Prompt only for a plan produced by this turn.  The
                        # session deliberately remains read-only until the
                        # user confirms execution below.
                        plan_pending = True

                    elif isinstance(event, TurnCompleteEvent):
                        await _stop_spinner_with_thinking()
                        _finish_stream_line()
                        _render_context_usage(event)

                    elif isinstance(event, SteeringAppliedEvent):
                        applied = composer.mark_guidance_applied(event.count)
                        if applied:
                            _finish_stream_line()
                        for guidance in applied:
                            _render_submitted_input(guidance, steering=True)

            except _REPL_INTERRUPT_EXCS:
                await spinner.stop()
                if ctrl_c_exit.should_exit_now():
                    console.print("\nGoodbye!", style="dim")
                    try:
                        await session.interrupt()
                    except Exception:
                        logger.debug("Failed to interrupt session while streaming on exit", exc_info=True)
                    _persist_partial_assistant_for_interrupt(
                        session, streamed_text_for_context
                    )
                    await _force_exit_cleanly()
                _clear_sigint_cancel()
                try:
                    await session.interrupt()
                except Exception:
                    logger.debug("Failed to interrupt session while streaming", exc_info=True)
                if streamed_text_for_context.strip():
                    _persist_partial_assistant_for_interrupt(
                        session, streamed_text_for_context
                    )
                composer.set_notice(
                    f"Interrupted · Ctrl+C again within "
                    f"{_CTRL_C_EXIT_WINDOW_S:.0f}s to exit"
                )
            finally:
                steering_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await steering_task

            if input_state.get("force_exit"):
                console.print("\nGoodbye!", style="dim")
                if streamed_text_for_context.strip():
                    _persist_partial_assistant_for_interrupt(
                        session,
                        streamed_text_for_context,
                    )
                await _force_exit_cleanly()

            if input_state.pop("interrupt_requested", False):
                if streamed_text_for_context.strip():
                    _persist_partial_assistant_for_interrupt(
                        session,
                        streamed_text_for_context,
                    )
                composer.set_notice(
                    f"Interrupted · Ctrl+C again within "
                    f"{_CTRL_C_EXIT_WINDOW_S:.0f}s to exit"
                )
            elif not composer.has_notice:
                composer.set_busy(False)

            if input_state.get("shutdown"):
                console.print("\nGoodbye!", style="dim")
                break

            if streamed_text:
                sys.stdout.write("\n")
                sys.stdout.flush()

            # Execute plan after the send_message generator has fully completed,
            # to avoid deadlock with the event queue inside send_message.
            if plan_pending:
                async with in_terminal():
                    await _prompt_plan_action(session, console)

            console.print()
    finally:
        if background_consumer is not None:
            background_consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_consumer
        try:
            await session.close()
        finally:
            if composer is not None:
                await composer.close()
            if stdout_patch is not None:
                stdout_patch.__exit__(None, None, None)


async def _prompt_plan_action(session: CoreSession, console: Console) -> None:
    """Prompt user to execute, modify, or cancel a pending plan."""
    from crabcode_core.plan.types import ExecutionPlan

    plan_data = session.current_plan
    if not plan_data:
        return

    plan = ExecutionPlan.from_dict(plan_data) if isinstance(plan_data, dict) else plan_data

    console.print()
    console.print(
        "  [bold]What would you like to do?[/]\n"
        "    [bold green]y[/] / [bold green]yes[/] — execute the plan\n"
        "    [bold blue]m[/] / [bold blue]modify[/] — request changes (stay in plan mode)\n"
        "    [bold red]n[/] / [bold red]no[/] — cancel the plan"
    )

    try:
        choice_session: PromptSession[str] = PromptSession()
        answer = await choice_session.prompt_async(
            HTML("<b><ansicyan>plan❯ </ansicyan></b>"),
        )
        answer = answer.strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("y", "yes", "execute", "run"):
        total_steps, stale_reset = plan.prepare_for_execution()
        first_wave = len(plan.get_ready_steps())
        session.set_plan(None)
        session.switch_mode("agent")
        stale_note = (
            f", {stale_reset} stale status(es) reset to pending"
            if stale_reset
            else ""
        )
        console.print(
            "\n  [bold cyan]Executing plan via DAG scheduler...[/]\n"
            f"  [dim]Plan confirmed (y): scheduling {total_steps} step(s)"
            f", {first_wave} runnable now{stale_note}[/]\n"
        )
        logger.info(
            "Plan confirmed (y): title=%r steps=%d first_wave=%d stale_reset=%d",
            plan.title,
            total_steps,
            first_wave,
            stale_reset,
        )
        try:
            await _run_plan_executor_with_runtime_events(session, plan)
        except _REPL_INTERRUPT_EXCS:
            _clear_sigint_cancel()
            console.print("\n  [dim yellow]Plan execution interrupted.[/]")
        console.print(f"\n  [dim]{plan.render()}[/]\n")

    elif answer in ("m", "modify", "edit", "change"):
        session.switch_mode("plan")
        console.print(
            "\n  [bold blue]Staying in plan mode.[/] "
            "Describe the changes you want — the plan will be revised.\n"
            "  [dim]The current plan is preserved in context.[/]"
        )

    else:
        session.set_plan(None)
        console.print("  [dim]Plan cancelled.[/]")


async def _handle_command(
    command: str,
    session: CoreSession,
    settings: CrabCodeSettings | None,
    pending_images: list[dict[str, str]] | None = None,
) -> bool | str:
    """Handle slash commands.

    Returns:
      True  — command handled, continue the REPL loop.
      False — exit requested.
      str   — expand the returned string as a user message (skill invocation).
    """
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # --- Built-in commands take priority over skill names ---
    skills = getattr(session, "skills", [])

    if cmd == "/image":
        if not arg:
            if pending_images is not None and pending_images:
                console.print(f"[dim]{len(pending_images)} image(s) attached for next message.[/]")
            else:
                console.print("[dim]Usage: /image <path> [path2 ...][/]")
            return True
        import base64
        import mimetypes
        from pathlib import Path

        paths = arg.split()
        added = 0
        for p in paths:
            img_path = Path(p).expanduser()
            if not img_path.exists():
                console.print(f"[bold red]File not found: {p}[/]")
                continue
            if img_path.stat().st_size > 20 * 1024 * 1024:
                console.print(f"[bold red]File too large (>20MB): {p}[/]")
                continue
            media_type = mimetypes.guess_type(str(img_path))[0] or "image/png"
            if not media_type.startswith("image/"):
                console.print(f"[bold red]Not an image: {p} (detected: {media_type})[/]")
                continue
            with open(img_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            if pending_images is not None:
                pending_images.append({"media_type": media_type, "data": data})
            added += 1
            size_kb = img_path.stat().st_size / 1024
            console.print(f"  [green]✓[/] Attached: {p} ({media_type}, {size_kb:.0f}KB)")
        if added and pending_images is not None:
            console.print(f"[dim]{len(pending_images)} image(s) will be sent with your next message.[/]")
        return True

    if cmd == "/help":
        skills_section = ""
        if skills:
            skill_lines = "\n".join(
                f"[bold]/{s.name}[/] — {s.description or s.when_to_use or 'skill'}"
                for s in skills
            )
            skills_section = f"\n\n[bold]Skills[/]\n{skill_lines}"
        console.print(Panel(
            "[bold]/help[/] — show this help\n"
            "[bold]/goal [objective][/] — set or view the persistent task goal\n"
            "[bold]/goal edit <objective>[/] — edit the current goal\n"
            "[bold]/goal pause|resume|complete|blocked|clear[/] — manage goal state\n"
            "[bold]/plan[/] — switch to plan mode (read-only analysis)\n"
            "[bold]/agent[/] — switch to agent mode (full execution; no args)\n"
            "[bold]/plan-status[/] — show current plan status\n"
            "[bold]/agents[/] — list managed agents\n"
            "[bold]/peers[/] — list messageable CrabCode sessions\n"
            "[bold]/tasks[/] — list background agents and monitors\n"
            "[bold]/tasks stop <id>[/] — stop a background task\n"
            "[bold]/agent <id>[/] — show a managed agent\n"
            "[bold]/agent-log <id>[/] — show an agent transcript\n"
            "[bold]/agent-send <id> <prompt>[/] — send more input to an agent\n"
            "[bold]/wait <id>[/] — wait for a managed agent\n"
            "[bold]/cancel-agent <id>[/] — cancel a managed agent\n"
            "[bold]/team list[/] — list active teams\n"
            "[bold]/team create <name> [max][/] — create a team\n"
            "[bold]/team status <id>[/] — show team status\n"
            "[bold]/team messages <id>[/] — show team messages\n"
            "[bold]/team tasks <id>[/] — show the team task board\n"
            "[bold]/team spawn <id> [options] <prompt>[/] — add a teammate\n"
            "[bold]/team message <id> <agent> <text>[/] — message a teammate\n"
            "[bold]/team broadcast <id> <text>[/] — message all teammates\n"
            "[bold]/team task-add <id> <description>[/] — add a team task\n"
            "[bold]/team task-claim <id> <task> [agent][/] — claim a task\n"
            "[bold]/team task-complete <id> <task> [result][/] — complete a task\n"
            "[bold]/team shutdown <id>[/] — shut down a team\n"
            "[bold]/schedule list [filters][/] — list scheduled tasks\n"
            "[bold]/schedule show <id>[/] — show a scheduled task\n"
            "[bold]/schedule runs <id> [filters][/] — show execution history\n"
            "[bold]/schedule create [options] <name> <type> <schedule> <prompt>[/] — create a task\n"
            "[bold]/schedule pause|resume|run|cancel <id>[/] — manage a task\n"
            "[bold]/status[/] — show session status (model, effort, ultra, context, compactions)\n"
            "[bold]/effort [none|minimal|low|medium|high|xhigh|max][/] — show/set reasoning effort\n"
            "[bold]/ultra [true|false][/] — toggle or explicitly set ultra mode\n"
            "[bold]/logs[/] — show background tool logs summary\n"
            "[bold]/logs <name>[/] — show a background log tail\n"
            "[bold]/logs --tail 200 <name>[/] — show more log lines\n"
            "[bold]/logs -f <name>[/] — follow a background log live\n"
            "[bold]/logs --clear <name>[/] — clear a background log\n"
            "[bold]/model[/] — show current model and list configured models\n"
            "[bold]/model <name>[/] — switch to a named model\n"
            "[bold]/new[/] — start a new session\n"
            "[bold]/compact[/] — compact conversation\n"
            "[bold]/clear[/] — clear conversation history\n"
            "[bold]/sessions[/] — list recent sessions\n"
            "[bold]/recent[/] — list recent sessions across all projects\n"
            "[bold]/search <query>[/] — search sessions by title or message\n"
            "[bold]/archive <id>[/] — archive a session\n"
            "[bold]/export [md|json] [path][/] — export current session\n"
            "[bold]/stats[/] — usage statistics\n"
            "[bold]/checkpoint [label][/] — create checkpoint (with file snapshot)\n"
            "[bold]/checkpoints[/] — list checkpoints\n"
            "[bold]/rollback <id|#>[/] — rollback conversation to a checkpoint\n"
            "[bold]/revert <id|#>[/] — revert files + conversation to a checkpoint\n"
            "[bold]/undo[/] — revert last checkpoint (files + conversation)\n"
            "[bold]/resume <id>[/] — resume a previous session\n"
            "[bold]/image <path>[/] — attach image(s) to your next message\n"
            "[bold]/exit[/] — exit CrabCode\n"
            f"[bold]Ctrl+C[/] — interrupt; press again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit\n"
            "[bold]While working[/] — type and press Enter to steer after the next tool call\n"
            "\n"
            "[bold]! <cmd>[/] — run a shell command"
            + skills_section,
            title="[bold]Commands[/]",
            border_style="blue",
        ))
        return True

    if cmd == "/goal":
        import shlex

        def render_goal() -> None:
            goal = session.get_goal()
            if goal is None:
                console.print("[dim]No goal is set.[/]")
                return
            status_styles = {
                "active": "green",
                "paused": "yellow",
                "complete": "cyan",
                "blocked": "red",
            }
            style = status_styles.get(goal.status, "white")
            body = Text(goal.objective)
            body.append("\n\nStatus: ")
            body.append(goal.status, style=style)
            if goal.token_budget is not None:
                remaining = goal.remaining_tokens
                body.append(
                    f"\nTokens: {goal.tokens_used:,} / {goal.token_budget:,} "
                    f"({remaining:,} remaining)"
                )
            console.print(Panel(body, title="Goal", border_style=style, expand=False))

        if not arg or arg.lower() in {"show", "status", "view"}:
            render_goal()
            return True

        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            console.print(f"[bold red]Invalid goal command:[/] {exc}")
            return True
        if not tokens:
            render_goal()
            return True

        action = tokens[0].lower()
        if action in {"pause", "resume", "complete", "blocked"}:
            if len(tokens) != 1:
                console.print(f"[dim]Usage: /goal {action}[/]")
                return True
            status = {
                "pause": "paused",
                "resume": "active",
            }.get(action, action)
            try:
                session.update_goal(status)
            except (RuntimeError, ValueError) as exc:
                console.print(f"[bold red]{exc}[/]")
                return True
            render_goal()
            return True

        if action == "clear":
            if len(tokens) != 1:
                console.print("[dim]Usage: /goal clear[/]")
                return True
            session.clear_goal()
            console.print("[dim]Goal cleared.[/]")
            return True

        edit = action == "edit"
        if action in {"set", "edit"}:
            tokens = tokens[1:]

        token_budget: int | None = None
        budget_was_set = False
        objective_parts: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--no-budget":
                token_budget = None
                budget_was_set = True
                index += 1
                continue
            if token == "--budget":
                if index + 1 >= len(tokens):
                    console.print("[dim]Usage: /goal [set|edit] [--budget N] <objective>[/]")
                    return True
                try:
                    token_budget = int(tokens[index + 1])
                except ValueError:
                    console.print("[bold red]Goal token budget must be a positive integer.[/]")
                    return True
                budget_was_set = True
                index += 2
                continue
            if token.startswith("--budget="):
                try:
                    token_budget = int(token.split("=", 1)[1])
                except ValueError:
                    console.print("[bold red]Goal token budget must be a positive integer.[/]")
                    return True
                budget_was_set = True
                index += 1
                continue
            objective_parts.append(token)
            index += 1

        objective = " ".join(objective_parts).strip()
        if not objective:
            console.print("[dim]Usage: /goal [set|edit] [--budget N] <objective>[/]")
            return True
        try:
            if edit:
                if budget_was_set:
                    session.edit_goal(objective, token_budget=token_budget)
                else:
                    session.edit_goal(objective)
            else:
                session.create_goal(objective, token_budget=token_budget)
        except (RuntimeError, ValueError) as exc:
            console.print(f"[bold red]{exc}[/]")
            return True
        render_goal()
        return True

    if cmd == "/plan":
        session.switch_mode("plan")
        console.print("[bold blue]Switched to plan mode[/] — read-only, agent will only plan")
        return True

    if cmd == "/agent" and not arg:
        session.switch_mode("agent")
        console.print("[bold green]Switched to agent mode[/] — full tool access")
        return True

    if cmd == "/plan-status":
        plan_data = session.current_plan
        if plan_data:
            from crabcode_core.plan.types import ExecutionPlan
            plan = ExecutionPlan.from_dict(plan_data) if isinstance(plan_data, dict) else plan_data
            console.print(Panel(
                plan.render(),
                title="[bold]Current Plan[/]",
                border_style="blue",
                expand=False,
            ))
        else:
            console.print("[dim]No active plan.[/]")
        mode = getattr(session, '_agent_mode', 'agent')
        console.print(f"  Mode: [bold]{'plan' if mode == 'plan' else 'agent'}[/]")
        return True

    if cmd == "/logs":
        try:
            from crabcode_search.background import list_background_logs, read_background_status

            logs = list_background_logs(session.cwd)
            bg_status = read_background_status(session.cwd)
        except Exception:
            logger.debug("Failed to load background logs metadata", exc_info=True)
            logs = {}
            bg_status = None

        follow, clear, tail, name, parse_error = _parse_logs_args(arg)
        if parse_error:
            console.print(f"[dim]Usage error: {parse_error}[/]")
            return True

        if not name:
            if not logs:
                console.print("[dim]No background logs found.[/]")
                return True

            lines = []
            for name, path_str in sorted(logs.items()):
                log_path = Path(path_str)
                try:
                    mtime = log_path.stat().st_mtime
                except OSError:
                    mtime = None
                meta: list[str] = [f"updated={_format_timestamp(mtime)}"]
                if name == "search" and bg_status:
                    state = bg_status.get("state")
                    if state:
                        meta.append(f"state={state}")
                lines.append(f"[bold]{name}[/] — {path_str} · {' · '.join(meta)}")

            console.print(Panel(
                "\n".join(lines),
                title="[bold]Background Logs[/]",
                border_style="blue",
                expand=False,
            ))
            return True

        try:
            path_str = logs[name]
        except KeyError:
            available = ", ".join(sorted(logs)) if logs else "(none)"
            console.print(f"[dim]Unknown log: {name}. Available: {available}[/]")
            return True

        log_path = Path(path_str)
        if clear:
            try:
                log_path.write_text("", encoding="utf-8")
            except OSError as exc:
                console.print(f"[bold red]Failed to clear log {name}: {exc}[/]")
                return True
            console.print(f"[dim]Cleared log: {name}[/]")
            if not follow:
                return True

        if follow:
            await _follow_log(log_path, name)
            return True

        body = _read_log_tail(log_path, max_lines=tail)
        console.print(Panel(
            Text(body, style="dim"),
            title=f"[bold]Log: {name}[/]",
            border_style="blue",
            expand=False,
        ))
        return True

    if cmd == "/model":
        if not arg:
            # Show current model
            current_name = getattr(session, "_current_model_name", None)
            active_cfg = session.settings.get_api_config(current_name)
            provider = active_cfg.provider
            model = active_cfg.model
            label = f"[bold cyan]{current_name}[/]  " if current_name else ""
            console.print(
                f"Current: {label}"
                f"provider=[bold]{provider or '[yellow]not set[/]'}[/]  "
                f"model=[bold]{model or '[yellow]not set[/]'}[/]"
            )
            named = session.list_models()
            if named:
                console.print("\nConfigured models (use [bold]/model <name>[/] to switch):")
                for name, desc in named.items():
                    marker = " [bold green]← active[/]" if name == current_name else ""
                    console.print(f"  [cyan]{name}[/]  {desc}{marker}")
            return True

        # /model <name>  — switch to named model
        named = session.list_models()
        if not named:
            console.print("[dim]No named models configured in settings. Add a [bold]models[/] section to settings.json.[/]")
            return True

        if arg not in named:
            console.print(f"[bold red]Unknown model: {arg}[/]  Available: {', '.join(named)}")
            return True

        ok = session.switch_model(arg)
        if ok:
            active_cfg = session.settings.models[arg]
            console.print(
                f"[green]✓[/] Switched to [bold cyan]{arg}[/]  "
                f"({active_cfg.provider or 'anthropic'}/{active_cfg.model or 'default'})"
            )
        else:
            console.print(f"[bold red]Failed to switch model to: {arg}[/]")
        return True

    if cmd == "/effort":
        if not arg:
            current = session.reasoning_effort or "auto"
            available = " | ".join(REASONING_EFFORT_LEVELS)
            console.print(
                f"Reasoning effort: [bold cyan]{current}[/]  "
                f"[dim](set with /effort <{available}>)[/]"
            )
            return True

        effort = arg.lower()
        if not session.set_reasoning_effort(effort):
            available = " | ".join(REASONING_EFFORT_LEVELS)
            console.print(
                f"[bold red]Invalid effort: {arg}[/]  "
                f"[dim]Usage: /effort <{available}>[/]"
            )
            return True
        console.print(
            f"[green]✓[/] Reasoning effort set to [bold cyan]{effort}[/] "
            "for subsequent requests."
        )
        return True

    if cmd == "/ultra":
        enabled: bool | None = None
        if arg:
            value = arg.lower()
            if value not in {"true", "false"}:
                console.print(
                    f"[bold red]Invalid ultra mode value: {arg}[/]  "
                    "[dim]Usage: /ultra [true|false][/]"
                )
                return True
            enabled = value == "true"

        ultra_enabled = session.set_ultra_mode(enabled)
        state = "on" if ultra_enabled else "off"
        style = "bold green" if ultra_enabled else "bold yellow"
        console.print(
            f"[green]✓[/] Ultra mode is now [{style}]{state}[/] "
            "for subsequent requests."
        )
        return True

    if cmd == "/status":
        from crabcode_core.api.model_info import DEFAULT_CONTEXT_WINDOW, lookup_context_window
        from crabcode_core.compact.compact import estimate_token_count

        initialized = getattr(session, "_initialized", False)

        current_name = getattr(session, "_current_model_name", None)
        active_cfg = session.settings.get_api_config(current_name)
        provider = active_cfg.provider or "anthropic"
        model = active_cfg.model or "unknown"
        model_display = f"{current_name} → " if current_name else ""
        model_display += f"{provider}/{model}"

        ctx_used = getattr(session, "last_context_used_tokens", 0) or estimate_token_count(session.messages)
        ctx_window = (
            getattr(session, "last_context_window_tokens", 0)
            or session.settings.max_context_length
            or active_cfg.context_window
            or lookup_context_window(active_cfg.model)
            or DEFAULT_CONTEXT_WINDOW
        )
        ctx_pct = int(ctx_used / ctx_window * 100) if ctx_window else 0

        def _fmt_k(n: int) -> str:
            return f"{n // 1000}k" if n >= 1000 else str(n)

        thinking = "on" if active_cfg.thinking_enabled else "off"
        effort = session.reasoning_effort or "auto"
        ultra = "on" if session.ultra_mode else "off"
        max_tok = active_cfg.max_tokens

        sid = session.session_id or "(none)"
        sid_short = sid[:8] + "…" if len(sid) > 8 else sid
        msg_count = len(session.messages)
        compact_count = getattr(session, "compact_count", 0)
        auto_compact = "on" if session.settings.auto_compact_enabled else "off"
        search_status = None

        if initialized:
            tool_count = len([t for t in session.tools if t.is_enabled])
            tool_display = str(tool_count)
        else:
            tool_display = "[dim]not loaded[/]"

        if "crabcode_search.CodebaseSearchTool" in session.settings.extra_tools:
            try:
                from crabcode_search.background import read_background_status

                bg_status = read_background_status(session.cwd)
            except Exception:
                logger.debug("Failed to read CodebaseSearch background status", exc_info=True)
                bg_status = None

            if bg_status:
                state = bg_status.get("state", "unknown")
                if state == "ready":
                    chunks = bg_status.get("chunks")
                    files = bg_status.get("files")
                    details: list[str] = []
                    if chunks is not None:
                        details.append(f"{chunks} chunks")
                    if files is not None:
                        details.append(f"{files} files")
                    suffix = f" ({', '.join(details)})" if details else ""
                    search_status = f"ready{suffix}"
                elif state == "indexing":
                    done = bg_status.get("done")
                    total = bg_status.get("total")
                    if isinstance(done, int) and isinstance(total, int) and total > 0:
                        pct = int(done / total * 100)
                        search_status = f"indexing {done}/{total} ({pct}%)"
                    else:
                        search_status = "indexing"
                else:
                    search_status = str(state)
            else:
                search_status = "enabled, waiting to start"

        agent_mode = getattr(session, '_agent_mode', 'agent')
        mode_display = "[bold blue]plan[/]" if agent_mode == "plan" else "[bold green]agent[/]"
        lines = [
            f"[bold cyan]🦀 CrabCode[/] v{__import__('crabcode_cli').__version__}",
            f"[bold]🧠 Model:[/] {model_display} · [bold]Mode:[/] {mode_display}",
            f"[bold]📚 Context:[/] {_fmt_k(ctx_used)} / {_fmt_k(ctx_window)} ({ctx_pct}%) · [bold]💬 Messages:[/] {msg_count}",
            f"[bold]🧹 Compactions:[/] {compact_count} · [bold]Auto-compact:[/] {auto_compact}",
            f"[bold]🧵 Session:[/] {sid_short}",
            f"[bold]⚙️  Config:[/] effort={effort} · ultra={ultra} · "
            f"think={thinking} · max_tokens={max_tok} · tools={tool_display}",
        ]
        agents = session.list_agents()
        if agents:
            active_agents = sum(1 for item in agents if item.status in {"queued", "running"})
            failed_agents = sum(1 for item in agents if item.status == "failed")
            pending_callbacks = sum(
                1
                for item in agents
                if item.callback_enabled
                and item.callback_state in {"pending", "injected"}
            )
            lines.append(
                f"[bold]🤖 Agents:[/] total={len(agents)} · active={active_agents} "
                f"· failed={failed_agents} · callbacks={pending_callbacks} "
                f"· max_concurrency={session.settings.agent.max_concurrency}"
            )
        monitors = session.list_monitor_tasks()
        if monitors:
            active_monitors = sum(1 for item in monitors if item.status == "running")
            failed_monitors = sum(1 for item in monitors if item.status == "failed")
            lines.append(
                f"[bold]📡 Monitors:[/] total={len(monitors)} "
                f"· active={active_monitors} · failed={failed_monitors}"
            )
        if search_status is not None:
            lines.append(f"[bold]🔎 Search:[/] {search_status}")
        console.print(Panel(
            "\n".join(lines),
            title="[bold]Status[/]",
            border_style="cyan",
            expand=False,
        ))
        return True

    if cmd == "/tasks":
        await session.initialize()
        if arg.startswith("stop "):
            task_id = arg[5:].strip()
            if not task_id:
                console.print("[dim]Usage: /tasks stop <task-id>[/]")
                return True
            stopped = await session.stop_background_task(task_id)
            if stopped:
                console.print(f"[green]Stopped background task {task_id}.[/]")
            else:
                console.print(f"[bold red]No running background task: {task_id}[/]")
            return True

        agents = session.list_agents()
        monitors = session.list_monitor_tasks()
        if not agents and not monitors:
            console.print("[dim]No background tasks.[/]")
            return True
        from rich.table import Table

        table = Table(title="Background Tasks", border_style="blue", expand=False)
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Status", style="dim", width=10)
        table.add_column("Type", style="dim", width=12)
        table.add_column("Description")
        for snapshot in monitors[:20]:
            table.add_row(
                snapshot.task_id[:8],
                snapshot.status,
                "monitor",
                snapshot.description[:60],
            )
        for snapshot in agents[:20]:
            table.add_row(
                snapshot.agent_id[:8],
                snapshot.status,
                "agent",
                snapshot.title[:60],
            )
        console.print(table)
        return True

    if cmd == "/agents":
        await session.initialize()
        agents = session.list_agents()
        if not agents:
            console.print("[dim]No managed agents.[/]")
            return True
        from rich.table import Table
        table = Table(title="Managed Agents", border_style="blue", expand=False)
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Status", style="dim", width=10)
        table.add_column("Type", style="dim", width=14)
        table.add_column("Depth", style="dim", width=5)
        table.add_column("Callback", style="dim", width=10)
        table.add_column("Title")
        for snapshot in agents[:20]:
            table.add_row(
                snapshot.agent_id[:8],
                snapshot.status,
                snapshot.subagent_type,
                str(snapshot.depth),
                snapshot.callback_state if snapshot.callback_enabled else "—",
                snapshot.title[:60],
            )
        console.print(table)
        return True

    if cmd == "/peers":
        try:
            runtime = await session.ensure_peer_runtime()
        except Exception as exc:
            console.print(f"[bold red]Cross-session messaging unavailable: {exc}[/]")
            return True
        peers = runtime.list_peers() if runtime is not None else []
        if not peers:
            console.print("[dim]No other messageable sessions.[/]")
            return True
        from rich.table import Table
        table = Table(title="CrabCode Session Peers", border_style="blue", expand=False)
        table.add_column("Name", style="cyan")
        table.add_column("Session", style="dim", width=10)
        table.add_column("Permissions", style="dim", width=12)
        table.add_column("Working Directory")
        for peer in peers[:50]:
            table.add_row(
                peer.name,
                peer.session_id[:8],
                peer.permission_class,
                peer.cwd,
            )
        console.print(table)
        return True

    if cmd == "/agent":
        await session.initialize()
        if not arg:
            console.print("[dim]Usage: /agent <agent-id>[/]")
            return True
        snapshot = session.get_agent(arg) or next(
            (item for item in session.list_agents() if item.agent_id.startswith(arg)),
            None,
        )
        if not snapshot:
            console.print(f"[bold red]Unknown agent: {arg}[/]")
            return True
        from crabcode_core.agent_manager import AgentManager
        console.print(
            Panel(
                Text(
                    AgentManager.format_snapshot(
                        snapshot,
                        max_result_chars=session.settings.agent.max_output_chars,
                    ),
                    style="dim",
                ),
                title=f"[bold]Agent {snapshot.agent_id[:8]}[/]",
                border_style="cyan",
                expand=False,
            )
        )
        return True

    if cmd == "/agent-log":
        await session.initialize()
        if not arg:
            console.print("[dim]Usage: /agent-log <agent-id>[/]")
            return True
        snapshot = session.get_agent(arg) or next(
            (item for item in session.list_agents() if item.agent_id.startswith(arg)),
            None,
        )
        if not snapshot:
            console.print(f"[bold red]Unknown agent: {arg}[/]")
            return True
        if not snapshot.transcript_path:
            console.print("[dim]This agent has no transcript path.[/]")
            return True
        path = Path(snapshot.transcript_path)
        if not path.exists():
            console.print(f"[dim]Transcript not found: {path}[/]")
            return True
        body = _read_log_tail(path, max_lines=200)
        console.print(
            Panel(
                Text(body, style="dim"),
                title=f"[bold]Agent Log {snapshot.agent_id[:8]}[/]",
                border_style="blue",
                expand=False,
            )
        )
        return True

    if cmd == "/agent-send":
        await session.initialize()
        if not arg or " " not in arg.strip():
            console.print("[dim]Usage: /agent-send <agent-id> <prompt>[/]")
            return True
        agent_ref, prompt = arg.split(None, 1)
        snapshot = session.get_agent(agent_ref) or next(
            (item for item in session.list_agents() if item.agent_id.startswith(agent_ref)),
            None,
        )
        if not snapshot:
            console.print(f"[bold red]Unknown agent: {agent_ref}[/]")
            return True
        ok = await session.send_agent_input(snapshot.agent_id, prompt, interrupt=False)
        if ok:
            console.print(f"[green]✓[/] Sent input to agent {snapshot.agent_id[:8]}")
            if session.settings.agent.stream_send_input_output:
                await _stream_agent_until_done(session, snapshot.agent_id)
        else:
            console.print(f"[bold red]Failed to send input to agent {snapshot.agent_id[:8]}[/]")
        return True

    if cmd == "/wait":
        await session.initialize()
        if not arg:
            console.print("[dim]Usage: /wait <agent-id>[/]")
            return True
        agent = session.get_agent(arg) or next(
            (item for item in session.list_agents() if item.agent_id.startswith(arg)),
            None,
        )
        if not agent:
            console.print(f"[bold red]Unknown agent: {arg}[/]")
            return True
        snapshot = await session.wait_agent(agent.agent_id, timeout_ms=None)
        if not snapshot:
            console.print(f"[bold red]Failed to wait for agent: {arg}[/]")
            return True
        from crabcode_core.agent_manager import AgentManager
        console.print(
            Panel(
                Text(
                    AgentManager.format_snapshot(
                        snapshot,
                        max_result_chars=session.settings.agent.max_output_chars,
                    ),
                    style="dim",
                ),
                title=f"[bold]Agent {snapshot.agent_id[:8]}[/]",
                border_style="cyan",
                expand=False,
            )
        )
        return True

    if cmd == "/cancel-agent":
        await session.initialize()
        if not arg:
            console.print("[dim]Usage: /cancel-agent <agent-id>[/]")
            return True
        agent = session.get_agent(arg) or next(
            (item for item in session.list_agents() if item.agent_id.startswith(arg)),
            None,
        )
        if not agent:
            console.print(f"[bold red]Unknown agent: {arg}[/]")
            return True
        ok = await session.cancel_agent(agent.agent_id)
        if ok:
            console.print(f"[yellow]Cancelled agent {agent.agent_id[:8]}[/]")
        else:
            console.print(f"[dim]Agent {agent.agent_id[:8]} is not running.[/]")
        return True

    if cmd == "/team":
        import shlex

        await session.initialize()
        team_mgr = getattr(session, "_team_manager", None)
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            console.print(f"[bold red]Invalid team command:[/] {exc}")
            return True
        subcmd = tokens.pop(0).lower() if tokens else "list"
        usage = (
            "/team [list|create|status|messages|tasks|spawn|message|broadcast|"
            "task-add|task-claim|task-complete|shutdown] [args]"
        )

        if subcmd == "list":
            teams = team_mgr.list_teams() if team_mgr else []
            if not teams:
                console.print("[dim]No active teams.[/]")
            else:
                for tid in teams:
                    status = team_mgr.get_team_status(tid) if team_mgr else {}
                    count = status.get("teammate_count", "?")
                    state = status.get("state", "?")
                    console.print(f"  [cyan]{tid}[/] · {count} teammates · {state}")
            return True

        if team_mgr is None:
            console.print("[dim]Team manager not initialized.[/]")
            return True

        if subcmd == "create":
            if not tokens or len(tokens) > 2:
                console.print("[dim]Usage: /team create <name> [max-teammates][/]")
                return True
            max_teammates = None
            if len(tokens) == 2:
                try:
                    max_teammates = int(tokens[1])
                except ValueError:
                    console.print("[bold red]max-teammates must be a positive integer.[/]")
                    return True
            try:
                team_id = await team_mgr.create_team(
                    tokens[0],
                    max_teammates=max_teammates,
                )
            except (RuntimeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                return True
            console.print(f"[green]Created team '{team_id}'.[/]")
            return True

        if subcmd == "status":
            if len(tokens) != 1:
                console.print("[dim]Usage: /team status <team-id>[/]")
                return True
            team_id = tokens[0]
            status = team_mgr.get_team_status(team_id)
            if not status:
                console.print(f"[bold red]Team '{team_id}' not found.[/]")
                return True
            lines = [
                f"Team: {status['team_id']}  State: {status['state']}",
                f"Teammates: {status['teammate_count']}/{status['max_teammates']}",
            ]
            for t in status["teammates"]:
                name = t.get("name") or t["agent_id"][:8]
                lines.append(f"  {name} · {t['role']} · {t['state']}")
            tasks = status["tasks"]
            lines.append(
                f"Tasks: {tasks['total']} total "
                f"({tasks['pending']} pending, {tasks['claimed']} claimed, "
                f"{tasks['completed']} done, {tasks['failed']} failed)"
            )
            console.print(Panel(
                "\n".join(lines),
                title=f"[bold]Team: {team_id}[/]",
                border_style="blue",
                expand=False,
            ))
            return True

        if subcmd == "messages":
            if len(tokens) != 1:
                console.print("[dim]Usage: /team messages <team-id>[/]")
                return True
            team_id = tokens[0]
            # Show recent messages for all teammates
            status = team_mgr.get_team_status(team_id)
            if not status:
                console.print(f"[bold red]Team '{team_id}' not found.[/]")
                return True
            messages_by_id: dict[str, Any] = {}
            for t in status["teammates"]:
                aid = t["agent_id"]
                for message in team_mgr.get_all_messages(team_id, aid):
                    messages_by_id.setdefault(message.id, message)
            messages = sorted(
                messages_by_id.values(),
                key=lambda item: item.timestamp,
            )[-50:]
            all_msgs: list[str] = []
            for message in messages:
                direction = f"{message.from_agent[:8]} -> {message.to_agent[:8]}"
                read_flag = "" if message.read else " (unread)"
                all_msgs.append(f"  {direction}: {message.text[:100]}{read_flag}")
            if not all_msgs:
                console.print("[dim]No messages.[/]")
            else:
                console.print(Panel(
                    Text("\n".join(all_msgs), style="dim"),
                    title=f"[bold]Messages: {team_id}[/]",
                    border_style="blue",
                    expand=False,
                ))
            return True

        if subcmd == "tasks":
            if len(tokens) != 1:
                console.print("[dim]Usage: /team tasks <team-id>[/]")
                return True
            team_id = tokens[0]
            if not team_mgr.get_team_status(team_id):
                console.print(f"[bold red]Team '{team_id}' not found.[/]")
                return True
            tasks = team_mgr.list_tasks(team_id)
            if not tasks:
                console.print("[dim]No team tasks.[/]")
                return True
            from rich.table import Table

            table = Table(title=f"Team Tasks: {team_id}", border_style="blue", expand=False)
            table.add_column("ID", style="cyan", width=10)
            table.add_column("Status", style="dim", width=10)
            table.add_column("Assignee", style="dim", width=10)
            table.add_column("Description")
            for task in tasks:
                status = getattr(task.status, "value", str(task.status))
                table.add_row(
                    task.id[:8],
                    status,
                    (task.assignee or "")[:8],
                    task.description[:80],
                )
            console.print(table)
            return True

        if subcmd == "spawn":
            if len(tokens) < 2:
                console.print(
                    "[dim]Usage: /team spawn <team-id> "
                    "[--role ROLE] [--name NAME] [--model PROFILE] <prompt>[/]"
                )
                return True
            team_id = tokens.pop(0)
            role_value = "worker"
            teammate_name = None
            model_profile = None
            prompt_parts: list[str] = []
            index = 0
            while index < len(tokens):
                token = tokens[index]
                option, separator, inline_value = token.partition("=")
                if option in {"--role", "--name", "--model", "--model-profile"}:
                    if separator:
                        value = inline_value
                    elif index + 1 < len(tokens):
                        index += 1
                        value = tokens[index]
                    else:
                        console.print(f"[bold red]Missing value for {option}.[/]")
                        return True
                    if not value:
                        console.print(f"[bold red]Missing value for {option}.[/]")
                        return True
                    if option == "--role":
                        role_value = value
                    elif option == "--name":
                        teammate_name = value
                    else:
                        model_profile = value
                elif token.startswith("-"):
                    console.print(f"[bold red]Unknown option: {token}[/]")
                    return True
                else:
                    prompt_parts.append(token)
                index += 1
            if not prompt_parts:
                console.print("[dim]A teammate prompt is required.[/]")
                return True
            from crabcode_core.team.models import TeammateRole

            try:
                agent_id = await team_mgr.add_teammate(
                    team_id,
                    role=TeammateRole(role_value),
                    prompt=" ".join(prompt_parts),
                    name=teammate_name,
                    model_profile=model_profile,
                )
            except (RuntimeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                return True
            console.print(f"[green]Added teammate {agent_id[:8]} to '{team_id}'.[/]")
            return True

        if subcmd == "message":
            if len(tokens) < 3:
                console.print("[dim]Usage: /team message <team-id> <agent-id> <text>[/]")
                return True
            team_id, agent_id = tokens[:2]
            message = await team_mgr.send_message(
                team_id,
                "",
                agent_id,
                " ".join(tokens[2:]),
            )
            if message is None:
                console.print("[bold red]Team message delivery failed.[/]")
            else:
                console.print(f"[green]Sent team message {message.id[:8]}.[/]")
            return True

        if subcmd == "broadcast":
            if len(tokens) < 2:
                console.print("[dim]Usage: /team broadcast <team-id> <text>[/]")
                return True
            team_id = tokens[0]
            messages = await team_mgr.broadcast(team_id, "", " ".join(tokens[1:]))
            console.print(f"[green]Broadcast to {len(messages)} teammate(s).[/]")
            return True

        if subcmd == "task-add":
            if len(tokens) < 2:
                console.print("[dim]Usage: /team task-add <team-id> <description>[/]")
                return True
            try:
                task_id = await team_mgr.add_task(tokens[0], " ".join(tokens[1:]))
            except (RuntimeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                return True
            console.print(f"[green]Added team task {task_id[:8]}.[/]")
            return True

        if subcmd == "task-claim":
            if len(tokens) not in {2, 3}:
                console.print("[dim]Usage: /team task-claim <team-id> <task-id> [agent-id][/]")
                return True
            claimed = await team_mgr.claim_task(
                tokens[0],
                tokens[1],
                tokens[2] if len(tokens) == 3 else "",
            )
            if claimed:
                console.print(f"[green]Claimed team task {tokens[1][:8]}.[/]")
            else:
                console.print("[bold red]Task not found or already claimed.[/]")
            return True

        if subcmd == "task-complete":
            if len(tokens) < 2:
                console.print(
                    "[dim]Usage: /team task-complete <team-id> <task-id> "
                    "[--agent ID] [result][/]"
                )
                return True
            team_id, task_id = tokens[:2]
            rest = tokens[2:]
            agent_id = None
            result_parts: list[str] = []
            index = 0
            while index < len(rest):
                token = rest[index]
                option, separator, inline_value = token.partition("=")
                if option in {"--agent", "--agent-id"}:
                    if separator:
                        agent_id = inline_value
                    elif index + 1 < len(rest):
                        index += 1
                        agent_id = rest[index]
                    else:
                        console.print(f"[bold red]Missing value for {option}.[/]")
                        return True
                elif token.startswith("-"):
                    console.print(f"[bold red]Unknown option: {token}[/]")
                    return True
                else:
                    result_parts.append(token)
                index += 1
            completed = await team_mgr.complete_task(
                team_id,
                task_id,
                " ".join(result_parts),
                agent_id,
            )
            if completed:
                console.print(f"[green]Completed team task {task_id[:8]}.[/]")
            else:
                console.print("[bold red]Task not found or not in claimed state.[/]")
            return True

        if subcmd == "shutdown":
            if len(tokens) != 1:
                console.print("[dim]Usage: /team shutdown <team-id>[/]")
                return True
            team_id = tokens[0]
            ok = await team_mgr.shutdown_team(team_id)
            if ok:
                console.print(f"[green]Team '{team_id}' shut down.[/]")
            else:
                console.print(f"[bold red]Team '{team_id}' not found.[/]")
            return True

        console.print(f"[dim]Usage: {usage}[/]")
        return True

    if cmd == "/schedule":
        import shlex

        from crabcode_core.tools.schedule import (
            _format_job_brief,
            _format_job_detail,
            _format_run_brief,
            _model_dict,
        )

        await session.initialize()
        manager = getattr(session, "_schedule_manager", None)
        if manager is None:
            console.print("[bold red]Schedule manager is unavailable.[/]")
            return True
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            console.print(f"[bold red]Invalid schedule command:[/] {exc}")
            return True

        subcmd = tokens.pop(0).lower() if tokens else "list"
        usage = (
            "/schedule [list|show|runs|create|pause|resume|run|cancel] [args]"
        )

        def option_value(
            values: list[str],
            index: int,
            token: str,
        ) -> tuple[str, int]:
            _, separator, inline = token.partition("=")
            if separator:
                if not inline:
                    raise ValueError(f"Missing value for {token.partition('=')[0]}")
                return inline, index
            if index + 1 >= len(values):
                raise ValueError(f"Missing value for {token}")
            return values[index + 1], index + 1

        if subcmd == "list":
            status = None
            schedule_type = None
            enabled = None
            limit = 100
            index = 0
            try:
                while index < len(tokens):
                    token = tokens[index]
                    option = token.partition("=")[0]
                    if option == "--status":
                        status, index = option_value(tokens, index, token)
                    elif option in {"--type", "--schedule-type"}:
                        schedule_type, index = option_value(tokens, index, token)
                        if schedule_type not in {"cron", "interval", "once"}:
                            raise ValueError("schedule type must be cron, interval, or once")
                    elif option == "--enabled":
                        raw_enabled, index = option_value(tokens, index, token)
                        if raw_enabled.lower() not in {"true", "false"}:
                            raise ValueError("enabled must be true or false")
                        enabled = raw_enabled.lower() == "true"
                    elif option == "--limit":
                        raw_limit, index = option_value(tokens, index, token)
                        limit = int(raw_limit)
                        if limit <= 0:
                            raise ValueError("limit must be greater than zero")
                    else:
                        raise ValueError(f"Unknown option: {token}")
                    index += 1
            except (TypeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                console.print(
                    "[dim]Usage: /schedule list [--status STATUS] "
                    "[--type cron|interval|once] [--enabled true|false] "
                    "[--limit N][/]"
                )
                return True

            try:
                jobs = manager.list_jobs(
                    status=status,
                    schedule_type=schedule_type,
                    enabled=enabled,
                    limit=limit,
                )
            except Exception as exc:
                console.print(f"[bold red]Failed to list schedules:[/] {safe_utf8_str(str(exc))}")
                return True
            if not jobs:
                console.print("[dim]No scheduled tasks found.[/]")
                return True
            lines = [_format_job_brief(_model_dict(job)) for job in jobs]
            console.print(
                Panel(
                    Text("\n".join(lines)),
                    title=f"[bold]Scheduled Tasks ({len(jobs)})[/]",
                    border_style="blue",
                    expand=False,
                )
            )
            return True

        if subcmd in {"show", "status"}:
            if len(tokens) != 1:
                console.print("[dim]Usage: /schedule show <job-id>[/]")
                return True
            try:
                job = manager.get_job(tokens[0])
            except Exception as exc:
                console.print(f"[bold red]Failed to read schedule:[/] {safe_utf8_str(str(exc))}")
                return True
            if job is None:
                console.print("[bold red]Scheduled task not found or ID is ambiguous.[/]")
                return True
            data = _model_dict(job)
            console.print(
                Panel(
                    Text(_format_job_detail(data)),
                    title=f"[bold]Schedule {str(data.get('id', ''))[:8]}[/]",
                    border_style="cyan",
                    expand=False,
                )
            )
            return True

        if subcmd in {"runs", "history"}:
            if not tokens:
                console.print(
                    "[dim]Usage: /schedule runs <job-id> "
                    "[--status STATUS] [--limit N][/]"
                )
                return True
            job_id = tokens.pop(0)
            status = None
            limit = 50
            index = 0
            try:
                while index < len(tokens):
                    token = tokens[index]
                    option = token.partition("=")[0]
                    if option == "--status":
                        status, index = option_value(tokens, index, token)
                    elif option == "--limit":
                        raw_limit, index = option_value(tokens, index, token)
                        limit = int(raw_limit)
                        if limit <= 0:
                            raise ValueError("limit must be greater than zero")
                    else:
                        raise ValueError(f"Unknown option: {token}")
                    index += 1
            except (TypeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                console.print(
                    "[dim]Usage: /schedule runs <job-id> "
                    "[--status STATUS] [--limit N][/]"
                )
                return True
            try:
                job = manager.get_job(job_id)
                runs = (
                    manager.list_runs(job_id, status=status, limit=limit)
                    if job is not None
                    else []
                )
            except Exception as exc:
                console.print(f"[bold red]Failed to read schedule runs:[/] {safe_utf8_str(str(exc))}")
                return True
            if job is None:
                console.print("[bold red]Scheduled task not found or ID is ambiguous.[/]")
                return True
            if not runs:
                console.print("[dim]No execution history found.[/]")
                return True
            lines = [_format_run_brief(_model_dict(run)) for run in runs]
            console.print(
                Panel(
                    Text("\n".join(lines)),
                    title=f"[bold]Schedule Runs ({len(runs)})[/]",
                    border_style="blue",
                    expand=False,
                )
            )
            return True

        if subcmd == "create":
            positionals: list[str] = []
            tags: list[str] = []
            cwd = None
            enabled = True
            max_runs = None
            next_run = None
            description = ""
            timeout = None
            model_profile = None
            job_session_id = None
            extra: dict[str, Any] = {}
            index = 0
            try:
                while index < len(tokens):
                    token = tokens[index]
                    if token == "--":
                        positionals.extend(tokens[index + 1 :])
                        break
                    option = token.partition("=")[0]
                    if option == "--disabled":
                        if "=" in token:
                            raise ValueError("--disabled does not accept a value")
                        enabled = False
                        index += 1
                        continue
                    if option in {
                        "--cwd",
                        "--enabled",
                        "--max-runs",
                        "--next-run",
                        "--description",
                        "--tag",
                        "--timeout",
                        "--model",
                        "--model-profile",
                        "--job-session",
                        "--run-session",
                        "--extra",
                    }:
                        value, index = option_value(tokens, index, token)
                        if option == "--cwd":
                            cwd = value
                        elif option == "--enabled":
                            if value.lower() not in {"true", "false"}:
                                raise ValueError("enabled must be true or false")
                            enabled = value.lower() == "true"
                        elif option == "--max-runs":
                            max_runs = int(value)
                            if max_runs <= 0:
                                raise ValueError("max-runs must be greater than zero")
                        elif option == "--next-run":
                            next_run = value
                        elif option == "--description":
                            description = value
                        elif option == "--tag":
                            tags.append(value)
                        elif option == "--timeout":
                            timeout = int(value)
                            if timeout <= 0:
                                raise ValueError("timeout must be greater than zero")
                        elif option in {"--model", "--model-profile"}:
                            model_profile = value
                        elif option in {"--job-session", "--run-session"}:
                            job_session_id = value
                        else:
                            parsed_extra = json.loads(value)
                            if not isinstance(parsed_extra, dict):
                                raise ValueError("extra must be a JSON object")
                            extra.update(parsed_extra)
                    elif token.startswith("-"):
                        raise ValueError(f"Unknown option: {token}")
                    else:
                        positionals.append(token)
                    index += 1
            except (TypeError, ValueError) as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
                console.print(
                    "[dim]Usage: /schedule create [options] <name> "
                    "<cron|interval|once> <schedule> <prompt>[/]"
                )
                return True

            if len(positionals) < 4:
                console.print(
                    "[dim]Usage: /schedule create [options] <name> "
                    "<cron|interval|once> <schedule> <prompt>[/]"
                )
                return True
            name, schedule_type, schedule_value = positionals[:3]
            if schedule_type not in {"cron", "interval", "once"}:
                console.print("[bold red]Schedule type must be cron, interval, or once.[/]")
                return True
            prompt = " ".join(positionals[3:])
            try:
                job = manager.create_job(
                    name=name,
                    prompt=prompt,
                    schedule=schedule_value,
                    schedule_type=schedule_type,
                    cwd=cwd,
                    enabled=enabled,
                    max_runs=max_runs,
                    next_run=next_run,
                    description=description,
                    tags=tags,
                    timeout=timeout,
                    model_profile=model_profile,
                    session_id=job_session_id,
                    extra=extra,
                )
            except Exception as exc:
                console.print(f"[bold red]Failed to create schedule:[/] {safe_utf8_str(str(exc))}")
                return True
            data = _model_dict(job)
            console.print(
                Panel(
                    Text(_format_job_detail(data)),
                    title="[bold]Scheduled Task Created[/]",
                    border_style="green",
                    expand=False,
                )
            )
            return True

        lifecycle_aliases = {
            "trigger": "run",
            "delete": "cancel",
        }
        action = lifecycle_aliases.get(subcmd, subcmd)
        if action in {"pause", "resume", "run", "cancel"}:
            if len(tokens) != 1:
                console.print(f"[dim]Usage: /schedule {subcmd} <job-id>[/]")
                return True
            job_id = tokens[0]
            try:
                if action == "pause":
                    job = manager.pause_job(job_id)
                    if job is None:
                        raise LookupError("Scheduled task not found or ID is ambiguous")
                    data = _model_dict(job)
                    console.print(f"[green]Scheduled task paused:[/] {data['name']} ({data['status']})")
                elif action == "resume":
                    job = manager.resume_job(job_id)
                    if job is None:
                        raise LookupError("Scheduled task not found or ID is ambiguous")
                    data = _model_dict(job)
                    console.print(f"[green]Scheduled task resumed:[/] {data['name']} ({data['status']})")
                elif action == "run":
                    started = await manager.trigger_job(job_id)
                    if not started:
                        raise RuntimeError(
                            "Scheduled task is missing, inactive, or already running"
                        )
                    console.print(f"[green]Scheduled task started:[/] {job_id[:8]}")
                else:
                    job = manager.get_job(job_id)
                    if job is None or not manager.cancel_job(job_id):
                        raise LookupError("Scheduled task not found or ID is ambiguous")
                    data = _model_dict(job)
                    console.print(f"[yellow]Scheduled task deleted:[/] {data['name']}")
            except Exception as exc:
                console.print(f"[bold red]{safe_utf8_str(str(exc))}[/]")
            return True

        console.print(f"[dim]Usage: {usage}[/]")
        return True

    if cmd == "/new":
        try:
            new_id = session.new_session()
        except RuntimeError as exc:
            console.print(f"[bold yellow]{safe_utf8_str(str(exc))}[/]")
            return True
        console.print(f"[dim]New session started: [bold]{new_id[:8]}…[/bold][/]")
        return True

    if cmd == "/compact":
        accepted = await session.compact(arg or None)
        if accepted:
            console.print("[dim]Conversation compacted (or queued at the active turn boundary).[/]")
        else:
            console.print("[dim]Not enough history to compact, or checkpoint generation failed.[/]")
        return True

    if cmd == "/clear":
        cleared = await session.clear_history()
        console.print(f"[dim]Conversation cleared ({cleared} message(s)).[/]")
        return True

    if cmd == "/sessions":
        from crabcode_core.session.storage import SessionStorage
        sessions = SessionStorage.list_sessions(session.cwd)
        if not sessions:
            console.print("[dim]No sessions found.[/]")
            return True
        from rich.table import Table
        table = Table(title="Recent Sessions", border_style="blue", expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan", width=8)
        table.add_column("Model", style="dim", width=16)
        table.add_column("Tokens", style="dim", width=8, justify="right")
        table.add_column("Modified", style="dim", width=16)
        table.add_column("Preview")
        for i, s in enumerate(sessions[:20], 1):
            sid = s["session_id"]
            is_current = sid == session.session_id
            marker = " *" if is_current else ""
            tokens = s.get("tokens_used", 0)
            tokens_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
            table.add_row(
                str(i),
                sid[:8] + marker,
                s.get("model", "")[:16],
                tokens_str,
                s.get("modified", "")[:16],
                s.get("preview", "")[:50],
            )
        console.print(table)
        return True

    if cmd == "/search":
        if not arg:
            console.print("[dim]Usage: /search <query>[/]")
            return True
        from crabcode_core.session.storage import SessionStorage
        results = SessionStorage.search_sessions(arg)
        if not results:
            console.print(f"[dim]No sessions matching \"{arg}\".[/]")
            return True
        from rich.table import Table as SearchTable
        table = SearchTable(title=f"Search: \"{arg}\"", border_style="blue", expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan", width=8)
        table.add_column("Project", style="dim", width=20)
        table.add_column("Model", style="dim", width=16)
        table.add_column("Tokens", style="dim", width=8, justify="right")
        table.add_column("Preview")
        for i, r in enumerate(results[:20], 1):
            sid = r.get("id", "")
            cwd_display = r.get("cwd", "")
            if len(cwd_display) > 20:
                cwd_display = "…" + cwd_display[-19:]
            tokens = r.get("tokens_used", 0)
            tokens_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
            preview = r.get("title", "") or r.get("first_user_message", "")
            table.add_row(
                str(i),
                sid[:8],
                cwd_display,
                r.get("model", "")[:16],
                tokens_str,
                preview[:50],
            )
        console.print(table)
        return True

    if cmd == "/stats":
        from crabcode_core.session.meta_db import SessionMetaStore as StatsStore
        store = StatsStore()
        try:
            g = store.stats_global()
            p = store.stats_by_project(os.path.abspath(session.cwd))
            models = store.stats_by_model(limit=5)
        finally:
            store.close()

        def _fmt_tok(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}k"
            return str(n)

        lines = [
            f"[bold]Global:[/]  {g['total_sessions']} sessions  |  "
            f"{_fmt_tok(g['total_tokens'])} tokens  |  "
            f"{g['active_projects']} projects",
            f"[bold]This week:[/]  {g['week_sessions']} sessions  |  "
            f"{_fmt_tok(g['week_tokens'])} tokens",
            f"[bold]This project:[/]  {p['total_sessions']} sessions  |  "
            f"{_fmt_tok(p['total_tokens'])} tokens  |  "
            f"{p['total_messages']} messages",
        ]
        if models:
            model_parts = [f"{m['model']} ({_fmt_tok(m['tokens'])})" for m in models]
            lines.append(f"[bold]Top models:[/]  {', '.join(model_parts)}")
        console.print(Panel(
            "\n".join(lines),
            title="[bold]Usage Statistics[/]",
            border_style="blue",
            expand=False,
        ))
        return True

    if cmd == "/checkpoint":
        label = arg or ""
        cp_id = session.checkpoint(label=label)
        if cp_id:
            label_display = f" \"{label}\"" if label else ""
            snap_indicator = " [dim](file snapshot included)[/]" if True else ""
            console.print(
                f"[green]✓[/] Checkpoint created{label_display}: [bold]{cp_id[:8]}…[/bold] "
                f"(at message {len(session.messages)}){snap_indicator}"
            )
        else:
            console.print("[dim]No active session or no messages to checkpoint.[/]")
        return True

    if cmd == "/checkpoints":
        cps = session.list_checkpoints()
        if not cps:
            console.print("[dim]No checkpoints for this session.[/]")
            return True
        from rich.table import Table as CpTable
        table = CpTable(title="Checkpoints", border_style="blue", expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan", width=8)
        table.add_column("Msg#", style="dim", width=5, justify="right")
        table.add_column("Files", style="green", width=5, justify="center")
        table.add_column("Label")
        table.add_column("Created", style="dim", width=20)
        for i, cp in enumerate(cps, 1):
            ts = cp.get("created_at", 0)
            created = _format_timestamp(ts) if ts else ""
            has_snap = "✓" if cp.get("snapshot_id") else "✗"
            table.add_row(
                str(i),
                cp["id"][:8],
                str(cp.get("message_index", "")),
                has_snap,
                cp.get("label", ""),
                created,
            )
        console.print(table)
        return True

    if cmd == "/rollback":
        if not arg:
            console.print("[dim]Usage: /rollback <checkpoint-id or #>[/]")
            return True
        cps = session.list_checkpoints()
        target_id = arg
        # Try numeric index first
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(cps):
                target_id = cps[idx]["id"]
        except ValueError:
            # Try prefix match
            for cp in cps:
                if cp["id"].startswith(arg):
                    target_id = cp["id"]
                    break
        old_count = len(session.messages)
        ok = session.rollback(target_id)
        if ok:
            console.print(
                f"[green]✓[/] Rolled back to checkpoint [bold]{target_id[:8]}…[/bold] "
                f"({old_count} → {len(session.messages)} messages)"
            )
        else:
            console.print(f"[bold red]Checkpoint not found: {arg}[/]")
        return True

    if cmd == "/revert" or cmd == "/undo":
        cps = session.list_checkpoints()
        if cmd == "/undo":
            # /undo = revert the most recent checkpoint
            if not cps:
                console.print("[dim]No checkpoints to undo.[/]")
                return True
            target_id = cps[0]["id"]
        else:
            if not arg:
                if not cps:
                    console.print("[dim]No checkpoints to revert.[/]")
                    return True
                target_id = cps[0]["id"]
            else:
                target_id = arg
                try:
                    idx = int(arg) - 1
                    if 0 <= idx < len(cps):
                        target_id = cps[idx]["id"]
                except ValueError:
                    for cp in cps:
                        if cp["id"].startswith(arg):
                            target_id = cp["id"]
                            break

        result = session.revert(target_id)
        if result.get("success"):
            parts = [f"[green]✓[/] Reverted to checkpoint [bold]{target_id[:8]}…[/bold]"]
            if result.get("files_restored"):
                parts.append(f"  [green]{len(result['files_restored'])} file(s) restored[/]")
            if result.get("messages_rolled_back"):
                parts.append(f"  [dim]{result['messages_rolled_back']} message(s) removed[/]")
            if result.get("warning"):
                parts.append(f"  [yellow]⚠ {result['warning']}[/]")
            console.print("\n".join(parts))
        else:
            console.print(f"[bold red]Checkpoint not found or revert failed: {target_id[:8]}[/]")
        return True

    if cmd == "/export":
        if not session.session_id:
            console.print("[dim]No active session to export.[/]")
            return True
        parts = arg.split() if arg else []
        fmt = "md"
        out_path = ""
        for p in parts:
            if p in ("md", "markdown", "json"):
                fmt = "json" if p == "json" else "md"
            else:
                out_path = p
        from crabcode_core.session.export import export_markdown, export_json
        if fmt == "json":
            content = export_json(session.session_id, session.cwd)
            ext = ".json"
        else:
            content = export_markdown(session.session_id, session.cwd)
            ext = ".md"
        if not out_path:
            out_path = os.path.join(session.cwd, f"{session.session_id[:8]}{ext}")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            console.print(f"[green]✓[/] Exported to [bold]{out_path}[/]")
        except OSError as exc:
            console.print(f"[bold red]Export failed: {exc}[/]")
        return True

    if cmd == "/archive":
        if not arg:
            console.print("[dim]Usage: /archive <session-id>[/]")
            return True
        from crabcode_core.session.storage import SessionStorage as ArchiveStorage
        sessions = ArchiveStorage.list_sessions(session.cwd)
        match = None
        for s in sessions:
            if s["session_id"] == arg or s["session_id"].startswith(arg):
                match = s["session_id"]
                break
        if not match:
            try:
                idx = int(arg) - 1
                if 0 <= idx < len(sessions):
                    match = sessions[idx]["session_id"]
            except ValueError:
                pass
        if not match:
            console.print(f"[bold red]Session not found: {arg}[/]")
            return True
        from crabcode_core.session.meta_db import SessionMetaStore as ArchiveStore
        store = ArchiveStore()
        try:
            store.archive(match)
        finally:
            store.close()
        console.print(f"[dim]Archived session [bold]{match[:8]}…[/bold][/]")
        return True

    if cmd == "/recent":
        from crabcode_core.session.meta_db import SessionMetaStore
        store = SessionMetaStore()
        try:
            rows = store.list_recent(limit=20)
        finally:
            store.close()
        if not rows:
            console.print("[dim]No sessions found.[/]")
            return True
        from rich.table import Table as RecentTable
        table = RecentTable(title="Recent Sessions (all projects)", border_style="blue", expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan", width=8)
        table.add_column("Project", style="dim", width=24)
        table.add_column("Model", style="dim", width=16)
        table.add_column("Tokens", style="dim", width=8, justify="right")
        table.add_column("Preview")
        for i, r in enumerate(rows, 1):
            sid = r.get("id", "")
            cwd_display = r.get("cwd", "")
            if len(cwd_display) > 24:
                cwd_display = "…" + cwd_display[-23:]
            tokens = r.get("tokens_used", 0)
            tokens_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
            preview = r.get("title", "") or r.get("first_user_message", "")
            table.add_row(
                str(i),
                sid[:8],
                cwd_display,
                r.get("model", "")[:16],
                tokens_str,
                preview[:50],
            )
        console.print(table)
        return True

    if cmd == "/resume":
        if not arg:
            console.print("[dim]Usage: /resume <session-id>[/]")
            return True

        from crabcode_core.session.storage import SessionStorage
        sessions = SessionStorage.list_sessions(session.cwd)
        session_id = arg

        match = None
        match_source = "local"
        # 1) Try current project: exact, prefix, or numeric index
        for s in sessions:
            if s["session_id"] == session_id or s["session_id"].startswith(session_id):
                match = s["session_id"]
                break
        if not match:
            try:
                idx = int(session_id) - 1
                if 0 <= idx < len(sessions):
                    match = sessions[idx]["session_id"]
            except ValueError:
                pass

        # 2) Fallback: cross-project lookup via SQLite
        if not match:
            from crabcode_core.session.meta_db import SessionMetaStore as ResumeStore
            store = ResumeStore()
            try:
                # Try exact match first
                row = store.get(session_id)
                if row:
                    match = row["id"]
                    match_source = row.get("cwd", "")
                else:
                    # Try prefix match across all recent sessions
                    recent = store.list_recent(limit=100)
                    for r in recent:
                        if r["id"].startswith(session_id):
                            match = r["id"]
                            match_source = r.get("cwd", "")
                            break
            finally:
                store.close()

        if not match:
            console.print(f"[bold red]Session not found: {session_id}[/]")
            return True

        if match_source != "local" and match_source:
            cwd_display = match_source
            if len(cwd_display) > 40:
                cwd_display = "…" + cwd_display[-39:]
            console.print(f"[dim]Found in project: {cwd_display}[/]")

        ok = await session.resume(match)
        if ok:
            console.print(
                f"[dim]Resumed session [bold]{match[:8]}…[/bold] "
                f"({len(session.messages)} messages)[/]"
            )
            console.print()
            _render_session_history(session.messages)
        else:
            console.print(f"[bold red]Failed to resume session {match[:8]}…[/]")
        return True

    if cmd in ("/exit", "/quit"):
        return False

    # --- Skill invocation: /<skill-name> [user input] ---
    skill_name = cmd.lstrip("/")
    matched_skill = next((s for s in skills if s.name == skill_name), None)
    if matched_skill:
        prompt = matched_skill.content
        if arg:
            if "$USER_INPUT" in prompt:
                prompt = prompt.replace("$USER_INPUT", arg)
            else:
                prompt = f"{prompt}\n\nUser input: {arg}"
        return prompt

    console.print(f"[dim]Unknown command: {command}. Type /help for available commands.[/]")
    return True
