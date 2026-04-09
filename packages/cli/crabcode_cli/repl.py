"""Interactive REPL — rich terminal UI with streaming, Markdown, and tool rendering."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from crabcode_cli.banner import print_banner
from crabcode_core.events import CoreSession
from crabcode_core.types.config import CrabCodeSettings, DisplaySettings
from crabcode_core.types.message import Message, MessageRole
from crabcode_core.utf8_sanitize import safe_utf8_str
from crabcode_core.types.event import (
    ChoiceRequestEvent,
    ChoiceResponseEvent,
    CompactEvent,
    ErrorEvent,
    PermissionRequestEvent,
    PermissionResponseEvent,
    StreamModeEvent,
    StreamTextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
)

# Module-level display settings, set during run_repl()
_display_settings: DisplaySettings | None = None


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


# Slash commands with their arguments for auto-completion
_SLASH_COMMANDS: dict[str, list[str]] = {
    "/help": [],
    "/status": [],
    "/logs": ["-f", "--follow", "--clear", "--tail"],
    "/model": [],  # Dynamic: model names
    "/new": [],
    "/compact": [],
    "/clear": [],
    "/sessions": [],
    "/resume": [],  # Dynamic: session IDs
    "/exit": [],
    "/quit": [],
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

            # /logs <name> — complete log names
            if cmd == "/logs":
                try:
                    from crabcode_search.background import list_background_logs
                    logs = list_background_logs(self._session.cwd if self._session else ".")
                except Exception:
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

    def _get_command_description(self, cmd: str) -> str:
        descriptions = {
            "/help": "show help",
            "/status": "show session status",
            "/logs": "show background logs",
            "/model": "show/switch model",
            "/new": "start new session",
            "/compact": "compact conversation",
            "/clear": "clear history",
            "/sessions": "list sessions",
            "/resume": "resume session",
            "/exit": "exit CrabCode",
            "/quit": "exit CrabCode",
        }
        return descriptions.get(cmd, "")


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_VERBS = ["Thinking", "Reasoning", "Analyzing", "Processing", "Understanding"]


def _force_exit(code: int = 130) -> None:
    """Exit immediately without waiting for executor/native thread cleanup."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


_CTRL_C_EXIT_WINDOW_S = 5.0


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

    def __init__(self, ansi_enabled: bool = True) -> None:
        self._ansi_enabled = ansi_enabled
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
    if name == "AskUser":
        question = inp.get("question", "")
        options = inp.get("options", [])
        lines = [question]
        for i, opt in enumerate(options, 1):
            lines.append(f"  {i}. {opt}")
        return "\n".join(lines)
    import json
    raw = json.dumps(inp, ensure_ascii=False)
    return (raw[:200] + "…") if len(raw) > 200 else raw


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
    console.print(
        Panel(
            Text(summary, style="dim"),
            title=f"[bold cyan]{event.tool_name}[/]",
            border_style="cyan",
            expand=False,
        )
    )


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
    console.print(
        Panel(
            Text(display, style="dim"),
            title=f"[bold {style}]{title}[/]",
            border_style=style,
            expand=False,
        )
    )


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
            if getattr(msg, "source_tool_assistant_uuid", None):
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
) -> None:
    """Prompt the user for tool permission and push response to session."""
    summary = _tool_summary(event.tool_name, event.tool_input)
    console.print(
        Panel(
            Text(summary, style="dim"),
            title=f"[bold yellow]⚠ {event.tool_name}[/]",
            border_style="yellow",
            expand=False,
        )
    )

    loop = asyncio.get_event_loop()
    while True:
        try:
            choice = await loop.run_in_executor(
                None,
                lambda: input(
                    f"  Allow {event.tool_name}? "
                    "(y)es / (n)o / (a)lways allow: "
                ).strip().lower(),
            )
        except (EOFError, KeyboardInterrupt):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=False
                )
            )
            return

        if choice in ("y", "yes", ""):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=True
                )
            )
            return
        elif choice in ("n", "no"):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id, allowed=False
                )
            )
            return
        elif choice in ("a", "always"):
            await session.respond_permission(
                PermissionResponseEvent(
                    tool_use_id=event.tool_use_id,
                    allowed=True,
                    always_allow=True,
                )
            )
            return
        else:
            console.print("  [dim]Please enter y, n, or a[/]")


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
                event.question,
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
                )
            )
        else:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=selected,
                )
            )
    else:
        # Fallback for non-interactive terminals: numbered text selection
        console.print(f"\n  [bold cyan]? {event.question}[/]")
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
                )
            )
        else:
            await session.respond_choice(
                ChoiceResponseEvent(
                    tool_use_id=event.tool_use_id,
                    selected=selected,
                )
            )


async def run_repl(
    settings: CrabCodeSettings | None = None,
    cwd: str = ".",
    resume_session_id: str | None = None,
) -> None:
    """Run the interactive REPL."""
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
        f"Ctrl+C interrupts; press again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit. "
        "Ctrl+D exits.",
        style="dim",
    )

    if settings and settings.permissions.run_everything:
        console.print()
        console.print(
            "  [bold yellow]⚠ WARNING: run_everything is enabled.[/] "
            "All tool calls will execute automatically without asking for permission.",
            style="yellow",
        )

    console.print()

    session = CoreSession(cwd=cwd, settings=settings)

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
        # Lazy initialization: session.initialize() is called by send_message().
        # Avoid background startup races that can garble terminal output and
        # block shutdown when Ctrl+C happens during native model loading.
        pass

    prompt_session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        completer=_CrabCodeCompleter(session),
        complete_while_typing=True,
    )

    ctrl_c_exit = _CtrlCDoubleExit()

    while True:
        try:
            user_input = await prompt_session.prompt_async(
                HTML("<b><ansicyan>❯ </ansicyan></b>"),
            )
        except EOFError:
            console.print("\nGoodbye!", style="dim")
            try:
                await session.interrupt()
            except Exception:
                pass
            _force_exit()
        except _REPL_INTERRUPT_EXCS:
            if ctrl_c_exit.should_exit_now():
                console.print("\nGoodbye!", style="dim")
                try:
                    await session.interrupt()
                except Exception:
                    pass
                _force_exit()
            _clear_sigint_cancel()
            try:
                await session.interrupt()
            except Exception:
                pass
            console.print(
                f"\n[dim]Interrupted. Press Ctrl+C again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit.[/]"
            )
            continue

        user_input = user_input.strip()
        if not user_input:
            continue

        ctrl_c_exit.clear()

        if user_input.startswith("/"):
            result = await _handle_command(user_input, session, settings)
            if result is False:
                break
            if isinstance(result, str):
                user_input = result
            else:
                continue

        if user_input.startswith("! "):
            cmd = user_input[2:]
            import subprocess
            result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=False)
            continue

        streamed_text = ""
        streamed_text_for_context = ""
        spinner = _Spinner(ansi_enabled=_ANSI_ENABLED)
        thinking_start: float = 0.0
        is_thinking = False

        async def _stop_spinner_with_thinking() -> None:
            nonlocal is_thinking
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

        try:
            async for event in session.send_message(user_input):
                if isinstance(event, StreamModeEvent):
                    if event.mode == "requesting":
                        spinner.start()
                        is_thinking = False
                        thinking_start = 0.0
                    elif event.mode == "thinking":
                        thinking_start = time.monotonic()
                        is_thinking = True
                        spinner.update("Thinking")
                    elif event.mode == "responding":
                        await _stop_spinner_with_thinking()
                    elif event.mode == "tool-input":
                        await _stop_spinner_with_thinking()
                        if streamed_text:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            streamed_text = ""
                        spinner.start("Generating")
                    elif event.mode == "tool-running":
                        await _stop_spinner_with_thinking()
                        if streamed_text:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            streamed_text = ""
                        spinner.start("Running")

                elif isinstance(event, ThinkingEvent):
                    pass

                elif isinstance(event, StreamTextEvent):
                    await _stop_spinner_with_thinking()
                    chunk = safe_utf8_str(event.text)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    streamed_text += chunk
                    streamed_text_for_context += event.text

                elif isinstance(event, ToolUseEvent):
                    if event.tool_name == "AskUser":
                        # Skip the ToolUse panel — ChoiceRequestEvent handles display
                        pass
                    else:
                        await _stop_spinner_with_thinking()
                        if streamed_text:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            streamed_text = ""
                        _render_tool_use(event)

                elif isinstance(event, PermissionRequestEvent):
                    await _stop_spinner_with_thinking()
                    if streamed_text:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        streamed_text = ""
                    await _prompt_permission(event, session)

                elif isinstance(event, ChoiceRequestEvent):
                    await _stop_spinner_with_thinking()
                    if streamed_text:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        streamed_text = ""
                    await _prompt_choice(event, session)

                elif isinstance(event, ToolResultEvent):
                    if event.tool_name == "AskUser":
                        # Show a concise selection result instead of raw JSON
                        await _stop_spinner_with_thinking()
                        if event.is_error:
                            console.print(f"  [dim yellow]↳ Selection cancelled[/]")
                        else:
                            console.print(f"  [dim green]↳ {safe_utf8_str(event.result)}[/]")
                    else:
                        await _stop_spinner_with_thinking()
                        _render_tool_result(event)

                elif isinstance(event, CompactEvent):
                    console.print(
                        f"\n[dim italic]Conversation compacted: {event.summary}[/]"
                    )

                elif isinstance(event, ErrorEvent):
                    await _stop_spinner_with_thinking()
                    console.print(
                        f"\n[bold red]Error: {safe_utf8_str(event.message)}[/]"
                    )
                    if not event.recoverable:
                        break

                elif isinstance(event, TurnCompleteEvent):
                    await _stop_spinner_with_thinking()

        except _REPL_INTERRUPT_EXCS:
            await spinner.stop()
            if ctrl_c_exit.should_exit_now():
                console.print("\nGoodbye!", style="dim")
                try:
                    await session.interrupt()
                except Exception:
                    pass
                _persist_partial_assistant_for_interrupt(
                    session, streamed_text_for_context
                )
                _force_exit()
            _clear_sigint_cancel()
            try:
                await session.interrupt()
            except Exception:
                pass
            _persist_partial_assistant_for_interrupt(
                session, streamed_text_for_context
            )
            console.print(
                f"[dim]Interrupted. Press Ctrl+C again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit.[/]"
            )

        if streamed_text:
            sys.stdout.write("\n")
            sys.stdout.flush()

        console.print()


async def _handle_command(
    command: str,
    session: CoreSession,
    settings: CrabCodeSettings | None,
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
            "[bold]/status[/] — show session status (model, context, compactions)\n"
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
            "[bold]/resume <id>[/] — resume a previous session\n"
            "[bold]/exit[/] — exit CrabCode\n"
            f"[bold]Ctrl+C[/] — interrupt; press again within {_CTRL_C_EXIT_WINDOW_S:.0f}s to exit\n"
            "\n"
            "[bold]! <cmd>[/] — run a shell command"
            + skills_section,
            title="[bold]Commands[/]",
            border_style="blue",
        ))
        return True

    if cmd == "/logs":
        try:
            from crabcode_search.background import list_background_logs, read_background_status

            logs = list_background_logs(session.cwd)
            bg_status = read_background_status(session.cwd)
        except Exception:
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

    if cmd == "/status":
        from crabcode_core.compact.compact import (
            DEFAULT_COMPACT_THRESHOLD,
            estimate_token_count,
        )

        initialized = getattr(session, "_initialized", False)

        current_name = getattr(session, "_current_model_name", None)
        active_cfg = session.settings.get_api_config(current_name)
        provider = active_cfg.provider or "anthropic"
        model = active_cfg.model or "unknown"
        model_display = f"{current_name} → " if current_name else ""
        model_display += f"{provider}/{model}"

        ctx_threshold = session.settings.max_context_length or DEFAULT_COMPACT_THRESHOLD
        ctx_used = estimate_token_count(session.messages)
        ctx_pct = int(ctx_used / ctx_threshold * 100) if ctx_threshold else 0

        def _fmt_k(n: int) -> str:
            return f"{n // 1000}k" if n >= 1000 else str(n)

        thinking = "on" if active_cfg.thinking_enabled else "off"
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

        lines = [
            f"[bold cyan]🦀 CrabCode[/] v{__import__('crabcode_cli').__version__}",
            f"[bold]🧠 Model:[/] {model_display}",
            f"[bold]📚 Context:[/] {_fmt_k(ctx_used)} / {_fmt_k(ctx_threshold)} ({ctx_pct}%) · [bold]💬 Messages:[/] {msg_count}",
            f"[bold]🧹 Compactions:[/] {compact_count} · [bold]Auto-compact:[/] {auto_compact}",
            f"[bold]🧵 Session:[/] {sid_short}",
            f"[bold]⚙️  Config:[/] think={thinking} · max_tokens={max_tok} · tools={tool_display}",
        ]
        if search_status is not None:
            lines.append(f"[bold]🔎 Search:[/] {search_status}")
        console.print(Panel(
            "\n".join(lines),
            title="[bold]Status[/]",
            border_style="cyan",
            expand=False,
        ))
        return True

    if cmd == "/new":
        new_id = session.new_session()
        console.print(f"[dim]New session started: [bold]{new_id[:8]}…[/bold][/]")
        return True

    if cmd == "/compact":
        await session.compact()
        console.print("[dim]Conversation compacted.[/]")
        return True

    if cmd == "/clear":
        session.messages.clear()
        console.print("[dim]Conversation cleared.[/]")
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

    if cmd == "/resume":
        if not arg:
            console.print("[dim]Usage: /resume <session-id>[/]")
            return True

        from crabcode_core.session.storage import SessionStorage
        sessions = SessionStorage.list_sessions(session.cwd)
        session_id = arg

        match = None
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

        if not match:
            console.print(f"[bold red]Session not found: {session_id}[/]")
            return True

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
