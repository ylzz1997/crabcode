"""CrabCode CLI entry point."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import typer
from crabcode_core.logging_utils import configure_logging, get_logger

app = typer.Typer(
    name="crabcode",
    help="CrabCode — AI coding assistant in the terminal",
    add_completion=False,
)

logger = get_logger(__name__)


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(None, help="Prompt to send (pipe mode)"),
    pipe: bool = typer.Option(False, "-p", "--pipe", help="Run in pipe mode (non-interactive)"),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model to use"),
    provider: Optional[str] = typer.Option(None, "--provider", help="API provider (anthropic/openai/codex/router)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (for routers)"),
    api_format: Optional[str] = typer.Option(None, "--api-format", help="API format for router (anthropic/openai)"),
    model_profile: Optional[str] = typer.Option(None, "--model-profile", "-M", help="Use a named model from settings.models"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
    resume: Optional[str] = typer.Option(None, "-r", "--resume", help="Resume a session by ID"),
    continue_last: bool = typer.Option(False, "-c", "--continue", help="Continue most recent session"),
) -> None:
    """CrabCode — AI coding assistant in the terminal."""
    from crabcode_core.types.config import ApiConfig, CrabCodeSettings

    work_dir = cwd or os.getcwd()

    settings = CrabCodeSettings()

    from crabcode_core.config.manager import ConfigManager
    file_settings = ConfigManager(cwd=work_dir).load()
    for key, val in file_settings.env.items():
        os.environ.setdefault(key, val)
    if file_settings.api.provider:
        settings.api.provider = file_settings.api.provider
    if file_settings.api.model:
        settings.api.model = file_settings.api.model
    if file_settings.api.base_url:
        settings.api.base_url = file_settings.api.base_url
    if file_settings.api.api_key_env:
        settings.api.api_key_env = file_settings.api.api_key_env
    if file_settings.api.format:
        settings.api.format = file_settings.api.format
    if not file_settings.api.thinking_enabled:
        settings.api.thinking_enabled = file_settings.api.thinking_enabled
    if file_settings.api.max_tokens != 16384:
        settings.api.max_tokens = file_settings.api.max_tokens

    if file_settings.models:
        settings.models = {**file_settings.models, **settings.models}
    if file_settings.default_model and not settings.default_model:
        settings.default_model = file_settings.default_model

    if file_settings.permissions.run_everything:
        settings.permissions.run_everything = True
    settings.logging = file_settings.logging.model_copy(deep=True)

    configure_logging(work_dir, settings.logging)

    if model:
        settings.api.model = model
    if provider:
        settings.api.provider = provider
    if base_url:
        settings.api.base_url = base_url
    if api_format:
        settings.api.format = api_format
    if model_profile:
        if model_profile in file_settings.models:
            settings.models = file_settings.models
            settings.default_model = model_profile
        else:
            typer.echo(
                f"Warning: model profile '{model_profile}' not found in settings.models. "
                f"Available: {list(file_settings.models.keys()) or '(none configured)'}",
                err=True,
            )

    stdin_text = ""
    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read().strip()

    if pipe or stdin_text:
        text = prompt or stdin_text
        if not text:
            typer.echo("Error: no prompt provided", err=True)
            raise typer.Exit(1)

        from crabcode_cli.pipe import run_pipe
        asyncio.run(run_pipe(text, settings=settings, cwd=work_dir))
        return

    if prompt:
        from crabcode_cli.pipe import run_pipe
        asyncio.run(run_pipe(prompt, settings=settings, cwd=work_dir))
        return

    resume_id: str | None = None
    if resume:
        resume_id = resume
    elif continue_last:
        from crabcode_core.session.storage import SessionStorage
        sessions = SessionStorage.list_sessions(work_dir)
        if sessions:
            resume_id = sessions[0]["session_id"]
        else:
            typer.echo("No previous sessions found.", err=True)

    if file_settings.extra_tools:
        try:
            from crabcode_search.background import (
                is_codebase_search_enabled,
                maybe_spawn_background_indexer,
            )

            if is_codebase_search_enabled(file_settings.extra_tools):
                maybe_spawn_background_indexer(
                    cwd=work_dir,
                    tool_config=file_settings.tool_settings.get("CodebaseSearch", {}),
                )
        except Exception:
            logger.exception("Failed to start background indexer bootstrap")

    from crabcode_cli.repl import run_repl
    asyncio.run(run_repl(settings=settings, cwd=work_dir, resume_session_id=resume_id))
    # Native library threads (PyTorch, FAISS) may keep the process alive
    # after the asyncio loop shuts down; force-exit to avoid hanging.
    os._exit(0)


def entry() -> None:
    app()


if __name__ == "__main__":
    entry()
