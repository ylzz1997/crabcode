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
    provider: Optional[str] = typer.Option(None, "--provider", help="API provider (anthropic/openai/codex/ollama/gemini/azure/bedrock/vertex/router)"),
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
    if file_settings.api.azure_endpoint:
        settings.api.azure_endpoint = file_settings.api.azure_endpoint
    if file_settings.api.azure_api_version:
        settings.api.azure_api_version = file_settings.api.azure_api_version
    if file_settings.api.azure_deployment:
        settings.api.azure_deployment = file_settings.api.azure_deployment

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


sessions_app = typer.Typer(
    name="sessions",
    help="Manage CrabCode sessions",
)
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    all_projects: bool = typer.Option(False, "--all", "-a", help="List sessions across all projects"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max number of sessions to show"),
) -> None:
    """List recent sessions."""
    work_dir = cwd or os.getcwd()
    if all_projects:
        from crabcode_core.session.meta_db import SessionMetaStore
        store = SessionMetaStore()
        rows = store.list_recent(limit=limit)
        store.close()
    else:
        from crabcode_core.session.storage import SessionStorage
        rows = SessionStorage.list_sessions(work_dir)[:limit]
        rows = [
            {"id": r["session_id"], "cwd": work_dir, **{k: v for k, v in r.items() if k != "session_id"}}
            for r in rows
        ]

    if not rows:
        typer.echo("No sessions found.")
        return

    for i, r in enumerate(rows, 1):
        sid = r.get("id", "")[:8]
        cwd_col = r.get("cwd", "")
        if len(cwd_col) > 30:
            cwd_col = "…" + cwd_col[-29:]
        model = r.get("model", "") or ""
        tokens = r.get("tokens_used", 0)
        tokens_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
        preview = r.get("title", "") or r.get("first_user_message", "") or r.get("preview", "")
        project_part = f"  [{cwd_col}]" if all_projects else ""
        typer.echo(f"  {i:>3}. {sid}  {model[:16]:<16}  {tokens_str:>6} tok{project_part}  {preview[:50]}")


@sessions_app.command("search")
def sessions_search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
) -> None:
    """Search sessions by title or first message."""
    from crabcode_core.session.storage import SessionStorage
    results = SessionStorage.search_sessions(query, limit=limit)
    if not results:
        typer.echo(f"No sessions matching \"{query}\".")
        return
    for i, r in enumerate(results, 1):
        sid = r.get("id", "")[:8]
        cwd_col = r.get("cwd", "")
        if len(cwd_col) > 30:
            cwd_col = "…" + cwd_col[-29:]
        model = r.get("model", "") or ""
        tokens = r.get("tokens_used", 0)
        tokens_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
        preview = r.get("title", "") or r.get("first_user_message", "")
        typer.echo(f"  {i:>3}. {sid}  {model[:16]:<16}  {tokens_str:>6} tok  [{cwd_col}]  {preview[:50]}")


@sessions_app.command("export")
def sessions_export(
    session_id: str = typer.Argument(..., help="Session ID (full or prefix)"),
    fmt: str = typer.Option("md", "--format", "-f", help="Export format: md or json"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
) -> None:
    """Export a session transcript to Markdown or JSON."""
    work_dir = cwd or os.getcwd()
    from crabcode_core.session.export import export_json, export_markdown
    if fmt == "json":
        content = export_json(session_id, work_dir)
        ext = ".json"
    else:
        content = export_markdown(session_id, work_dir)
        ext = ".md"
    out_path = output or os.path.join(work_dir, f"{session_id[:8]}{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    typer.echo(f"Exported to {out_path}")


@sessions_app.command("prune")
def sessions_prune(
    days: int = typer.Option(30, "--days", "-d", help="Archive sessions older than N days"),
    delete_files: bool = typer.Option(False, "--delete-files", help="Also delete JSONL transcript files"),
) -> None:
    """Archive old sessions and optionally delete their files."""
    from crabcode_core.session.meta_db import SessionMetaStore
    store = SessionMetaStore()
    archived = store.auto_archive(days=days)
    typer.echo(f"Archived {archived} session(s) older than {days} days.")
    if delete_files:
        purged = store.purge_archived()
        for entry in purged:
            sid = entry["id"]
            cwd = entry.get("cwd", "")
            if cwd:
                from crabcode_core.session.storage import get_transcript_path
                path = get_transcript_path(cwd, sid)
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
        typer.echo(f"Purged {len(purged)} archived session(s) from database and disk.")
    else:
        purged = store.purge_archived()
        typer.echo(f"Purged {len(purged)} archived session(s) from database.")
    store.close()


@app.command("gateway")
def gateway(
    port: int = typer.Option(4096, "--port", "-p", help="HTTP port"),
    grpc_port: Optional[int] = typer.Option(None, "--grpc-port", help="gRPC port (optional)"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    password: Optional[str] = typer.Option(None, "--password", help="Basic Auth password"),
    cors: Optional[str] = typer.Option(None, "--cors", help="Allowed CORS origin"),
    log_level: str = typer.Option("info", "--log-level", help="Log level"),
) -> None:
    """Start the CrabCode HTTP/gRPC gateway server."""
    from crabcode_core.logging_utils import configure_logging
    from crabcode_core.types.config import CrabCodeSettings, LoggingSettings

    log_settings = LoggingSettings(level=log_level.upper())
    configure_logging(os.getcwd(), log_settings)

    cors_origins = [cors] if cors else None

    from crabcode_gateway.server import run_server

    run_server(
        host=host,
        port=port,
        grpc_port=grpc_port,
        password=password,
        cors_origins=cors_origins,
        log_level=log_level,
    )


@app.command("acp")
def acp_cmd(
    port: int = typer.Option(4096, "--port", "-p", help="Internal HTTP server port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Internal HTTP server host"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
    log_level: str = typer.Option("warning", "--log-level", help="Log level"),
) -> None:
    """Start ACP (Agent Client Protocol) server on stdio.

    This command starts an internal Gateway HTTP server and then
    runs the ACP agent, communicating with the editor over
    stdin/stdout via JSON-RPC (ndjson).

    Used by ACP-compatible editors (Zed, JetBrains, Neovim) to
    integrate CrabCode as a coding agent backend.

    Example Zed settings.json::

        {
          "agent": {
            "profiles": {
              "crabcode": {
                "command": "crabcode",
                "args": ["acp"]
              }
            }
          }
        }
    """
    import asyncio

    from crabcode_core.logging_utils import configure_logging
    from crabcode_core.types.config import CrabCodeSettings, LoggingSettings

    work_dir = cwd or os.getcwd()
    log_settings = LoggingSettings(level=log_level.upper())
    configure_logging(work_dir, log_settings)

    async def _run() -> None:
        # 1. Start internal Gateway HTTP server
        from crabcode_gateway.server import GatewayServer

        server = GatewayServer(host=host, port=port, log_level=log_level)
        await server.start_background()

        # Wait for server to be ready
        import httpx
        async with httpx.AsyncClient() as health_client:
            for _ in range(20):
                try:
                    resp = await health_client.get(f"http://{host}:{port}/health")
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)

        # 2. Run ACP agent on stdio
        from crabcode_gateway.acp.transport import run_acp_server
        from crabcode_gateway.acp.types import ACPConfig

        config = ACPConfig(base_url=f"http://{host}:{port}")
        try:
            await run_acp_server(config)
        finally:
            await server.stop()

    asyncio.run(_run())
    os._exit(0)


@app.command("stats")
def stats(
    project: bool = typer.Option(False, "--project", "-p", help="Show only current project stats"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory"),
) -> None:
    """Show usage statistics."""
    work_dir = os.path.abspath(cwd or os.getcwd())
    from crabcode_core.session.meta_db import SessionMetaStore
    store = SessionMetaStore()

    def _fmt_tok(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    if project:
        p = store.stats_by_project(work_dir)
        typer.echo(f"Project: {work_dir}")
        typer.echo(f"  Sessions: {p['total_sessions']}  |  Tokens: {_fmt_tok(p['total_tokens'])}  |  Messages: {p['total_messages']}")
    else:
        g = store.stats_global()
        p = store.stats_by_project(work_dir)
        models = store.stats_by_model(limit=5)
        typer.echo(f"Global:        {g['total_sessions']} sessions  |  {_fmt_tok(g['total_tokens'])} tokens  |  {g['active_projects']} projects")
        typer.echo(f"This week:     {g['week_sessions']} sessions  |  {_fmt_tok(g['week_tokens'])} tokens")
        typer.echo(f"This project:  {p['total_sessions']} sessions  |  {_fmt_tok(p['total_tokens'])} tokens  |  {p['total_messages']} messages")
        if models:
            model_parts = [f"{m['model']} ({_fmt_tok(m['tokens'])})" for m in models]
            typer.echo(f"Top models:    {', '.join(model_parts)}")
    store.close()


def entry() -> None:
    known_subcommands = {"main", "sessions", "stats", "gateway", "acp"}
    args = sys.argv[1:]
    # Preserve root --help so users can still discover all subcommands.
    if args and args[0] in ("--help", "-h"):
        app()
        return
    # If the first positional argument is not a known subcommand, default to main.
    first_positional = next((a for a in args if not a.startswith("-")), None)
    if first_positional not in known_subcommands:
        sys.argv.insert(1, "main")
    app()


if __name__ == "__main__":
    entry()
