"""Message rendering utilities for the CLI."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


def render_assistant_markdown(text: str, console: Console | None = None) -> None:
    """Render assistant text as Markdown."""
    c = console or Console()
    c.print(Markdown(text))


def render_error(message: str, console: Console | None = None) -> None:
    """Render an error message."""
    c = console or Console()
    c.print(Panel(message, title="Error", border_style="red"))
