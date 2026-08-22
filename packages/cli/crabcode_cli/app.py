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

document_engine_app = typer.Typer(
    name="document-engine",
    help="Manage the optional local high-fidelity PDF translation engine.",
    add_completion=False,
)
app.add_typer(document_engine_app, name="document-engine")

logger = get_logger(__name__)


@document_engine_app.command("status")
def document_engine_status_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Show the optional BabelDOC engine status."""
    import json

    from crabcode_gateway.document_engine import document_engine_status

    status = document_engine_status()
    if json_output:
        typer.echo(json.dumps(status, ensure_ascii=False))
    else:
        typer.echo(f"{status['status']}: {status['detail']}")


@document_engine_app.command("install")
def document_engine_install_cmd(
    bundle: Optional[str] = typer.Option(
        None,
        "--bundle",
        help="Use an offline bundle path or URL instead of the official BabelDOC source",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Install BabelDOC from its official source, or use an offline bundle."""
    import json

    from crabcode_gateway.document_engine import install_document_engine

    def progress(message: str) -> None:
        if not json_output:
            typer.echo(message)

    try:
        status = install_document_engine(bundle, progress=progress)
    except Exception as exc:
        if json_output:
            typer.echo(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f"安装失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(status, ensure_ascii=False))
    else:
        typer.echo(status["detail"])


@document_engine_app.command("remove")
def document_engine_remove_cmd(
    yes: bool = typer.Option(False, "--yes", help="Remove without confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Remove the managed engine without touching translated project PDFs."""
    import json

    from crabcode_gateway.document_engine import remove_document_engine

    if not yes and not typer.confirm("删除本地高精度 PDF 引擎？已生成的译后 PDF 会保留。"):
        raise typer.Abort()
    status = remove_document_engine()
    if json_output:
        typer.echo(json.dumps(status, ensure_ascii=False))
    else:
        typer.echo(status["detail"])


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
    image: Optional[list[str]] = typer.Option(None, "-i", "--image", help="Image file(s) to attach (repeatable)"),
) -> None:
    """CrabCode — AI coding assistant in the terminal."""
    from crabcode_core.types.config import CrabCodeSettings

    work_dir = cwd or os.getcwd()

    # Resolve cross-project resumes before loading project settings or creating
    # any cwd-bound runtime resources.  CoreSession also rebinds an already
    # initialized session, but startup should take the correct project path
    # from the outset.
    if resume:
        from crabcode_core.session.storage import SessionStorage

        resolved_storage = SessionStorage.from_session_id(resume)
        if resolved_storage is not None:
            work_dir = resolved_storage.cwd

    settings = CrabCodeSettings()
    # Keep CLI flags separate from project-file values.  CoreSession needs
    # this distinction when an interactive REPL later resumes a session from a
    # different project; otherwise the first project's config is treated as a
    # caller override and shadows the target project's settings.
    explicit_settings = CrabCodeSettings()

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
    if file_settings.api.codex_auth_path:
        settings.api.codex_auth_path = file_settings.api.codex_auth_path
    if file_settings.api.http_headers:
        settings.api.http_headers = dict(file_settings.api.http_headers)
    if file_settings.api.format:
        settings.api.format = file_settings.api.format
    if not file_settings.api.thinking_enabled:
        settings.api.thinking_enabled = file_settings.api.thinking_enabled
    if file_settings.api.pass_reasoning_content:
        settings.api.pass_reasoning_content = file_settings.api.pass_reasoning_content
    if file_settings.api.reasoning_effort is not None:
        settings.api.reasoning_effort = file_settings.api.reasoning_effort
    if file_settings.api.max_tokens != 16384:
        settings.api.max_tokens = file_settings.api.max_tokens
    if file_settings.api.extra_body:
        settings.api.extra_body = dict(file_settings.api.extra_body)
    if file_settings.api.azure_endpoint:
        settings.api.azure_endpoint = file_settings.api.azure_endpoint
    if file_settings.api.azure_api_version:
        settings.api.azure_api_version = file_settings.api.azure_api_version
    if file_settings.api.azure_deployment:
        settings.api.azure_deployment = file_settings.api.azure_deployment
    if file_settings.ultra_mode:
        settings.ultra_mode = True
    if file_settings.tool_call_timeout is not None:
        settings.tool_call_timeout = file_settings.tool_call_timeout

    if file_settings.models:
        settings.models = {**file_settings.models, **settings.models}
    if file_settings.default_model and not settings.default_model:
        settings.default_model = file_settings.default_model

    if file_settings.permissions.default_mode and not settings.permissions.default_mode:
        settings.permissions.default_mode = file_settings.permissions.default_mode
    if file_settings.permissions.run_everything:
        settings.permissions.run_everything = True
    settings.logging = file_settings.logging.model_copy(deep=True)

    configure_logging(work_dir, settings.logging)

    if model:
        settings.api.model = model
        explicit_settings.api.model = model
    if provider:
        settings.api.provider = provider
        explicit_settings.api.provider = provider
    if base_url:
        settings.api.base_url = base_url
        explicit_settings.api.base_url = base_url
    if api_format:
        settings.api.format = api_format
        explicit_settings.api.format = api_format
    if model_profile:
        if model_profile in file_settings.models:
            settings.default_model = model_profile
            explicit_settings.default_model = model_profile
        else:
            typer.echo(
                f"Warning: model profile '{model_profile}' not found in settings.models. "
                f"Available: {list(file_settings.models.keys()) or '(none configured)'}",
                err=True,
            )

    settings._crabcode_explicit_settings = explicit_settings

    # Read image files into base64 attachments
    image_attachments: list[dict[str, str]] | None = None
    if image:
        import base64
        import mimetypes
        from pathlib import Path

        image_attachments = []
        for img_path_str in image:
            img_path = Path(img_path_str)
            if not img_path.exists():
                typer.echo(f"Error: image file not found: {img_path_str}", err=True)
                raise typer.Exit(1)
            if img_path.stat().st_size > 20 * 1024 * 1024:
                typer.echo(f"Error: image file too large (>20MB): {img_path_str}", err=True)
                raise typer.Exit(1)
            media_type = mimetypes.guess_type(img_path_str)[0] or "image/png"
            if not media_type.startswith("image/"):
                typer.echo(f"Error: not an image file: {img_path_str}", err=True)
                raise typer.Exit(1)
            with open(img_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            image_attachments.append({"media_type": media_type, "data": data})

    stdin_text = ""
    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read().strip()

    if pipe or stdin_text:
        text = prompt or stdin_text
        if not text:
            typer.echo("Error: no prompt provided", err=True)
            raise typer.Exit(1)

        from crabcode_cli.pipe import run_pipe
        asyncio.run(run_pipe(text, settings=settings, cwd=work_dir, images=image_attachments))
        return

    if prompt:
        from crabcode_cli.pipe import run_pipe
        asyncio.run(run_pipe(prompt, settings=settings, cwd=work_dir, images=image_attachments))
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
        try:
            rows = store.list_recent(limit=limit)
        finally:
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

    try:
        resolved_id, resolved_cwd = _resolve_export_session(session_id, work_dir)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if fmt not in {"md", "json"}:
        typer.echo("Error: --format must be 'md' or 'json'", err=True)
        raise typer.Exit(1)
    if fmt == "json":
        content = export_json(resolved_id, resolved_cwd)
        ext = ".json"
    else:
        content = export_markdown(resolved_id, resolved_cwd)
        ext = ".md"
    out_path = output or os.path.join(work_dir, f"{resolved_id[:8]}{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    typer.echo(f"Exported to {out_path}")


def _resolve_export_session(selector: str, cwd: str) -> tuple[str, str]:
    """Resolve an export selector without silently producing an empty file."""
    from crabcode_core.session.meta_db import SessionMetaStore
    from crabcode_core.session.storage import SessionStorage, get_transcript_path

    value = str(selector or "").strip()
    if not value:
        raise ValueError("session ID is required")

    work_dir = os.path.abspath(cwd)
    local_ids = [
        str(row.get("session_id") or "")
        for row in SessionStorage.list_sessions(work_dir)
        if row.get("session_id")
    ]
    if value in local_ids:
        return value, work_dir
    local_matches = [session_id for session_id in local_ids if session_id.startswith(value)]
    if len(local_matches) == 1:
        return local_matches[0], work_dir
    if len(local_matches) > 1:
        raise ValueError(f"session selector is ambiguous: {value}")

    store = SessionMetaStore()
    try:
        exact = store.get(value)
        if exact is not None:
            return value, str(exact.get("cwd") or work_dir)
        matches = store.find_active_by_prefix(value, limit=2)
    finally:
        store.close()
    if len(matches) == 1:
        return str(matches[0]["id"]), str(matches[0].get("cwd") or work_dir)
    if len(matches) > 1:
        raise ValueError(f"session selector is ambiguous: {value}")

    try:
        if get_transcript_path(work_dir, value).exists():
            return value, work_dir
    except (TypeError, ValueError):
        pass
    raise ValueError(f"session not found: {value}")


@sessions_app.command("prune")
def sessions_prune(
    days: int = typer.Option(30, "--days", "-d", help="Archive sessions older than N days"),
    delete_files: bool = typer.Option(False, "--delete-files", help="Also delete JSONL transcript files"),
) -> None:
    """Archive old sessions and optionally delete their files."""
    from crabcode_core.session.meta_db import SessionMetaStore
    store = SessionMetaStore()
    try:
        archived = store.auto_archive(days=days)
        typer.echo(f"Archived {archived} session(s) older than {days} days.")
        if delete_files:
            candidates = store.purge_archived(delete_rows=False)
            purged = 0
            failed: list[str] = []
            for entry in candidates:
                sid = entry["id"]
                cwd = entry.get("cwd", "")
                try:
                    if cwd:
                        from crabcode_core.session.storage import purge_session_artifacts

                        purge_session_artifacts(cwd, sid)
                    store.delete(sid)
                    purged += 1
                except (OSError, ValueError):
                    # Retain the archived row so a later prune can retry and so
                    # the command never claims a failed disk deletion succeeded.
                    failed.append(sid)
            typer.echo(f"Purged {purged} archived session(s) from database and disk.")
            if failed:
                typer.echo(
                    f"Failed to delete {len(failed)} session artifact set(s); "
                    "their archived database rows were retained for retry.",
                    err=True,
                )
    finally:
        store.close()


@app.command("gateway")
def gateway(
    port: int = typer.Option(4096, "--port", "-p", help="HTTP port"),
    grpc_port: Optional[int] = typer.Option(None, "--grpc-port", help="gRPC port (optional)"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    password: Optional[str] = typer.Option(None, "--password", help="Gateway password"),
    security_mode: Optional[str] = typer.Option(None, "--security-mode", help="none, password, publickey, or mixed"),
    password_hash: Optional[str] = typer.Option(None, "--password-hash", help="PBKDF2 password hash"),
    authorized_keys: Optional[str] = typer.Option(None, "--authorized-keys", help="OpenSSH authorized_keys path"),
    jwt_secret: Optional[str] = typer.Option(None, "--jwt-secret", help="JWT signing secret"),
    cors: Optional[str] = typer.Option(None, "--cors", help="Allowed CORS origin"),
    log_level: str = typer.Option("info", "--log-level", help="Log level"),
) -> None:
    """Start the CrabCode HTTP/gRPC gateway server."""
    from crabcode_core.logging_utils import configure_logging
    from crabcode_core.types.config import LoggingSettings

    log_settings = LoggingSettings(level=log_level.upper())
    configure_logging(os.getcwd(), log_settings)

    cors_origins = [cors] if cors else None

    from crabcode_gateway.server import run_server

    from crabcode_core.config.manager import ConfigManager
    overrides = {
        key: value
        for key, value in {
            "mode": security_mode,
            "password": password,
            "password_hash": password_hash,
            "authorized_keys": authorized_keys,
            "jwt_secret": jwt_secret,
        }.items()
        if value is not None
    }
    if password is not None and security_mode is None:
        overrides["mode"] = "password"
    configured = ConfigManager(cwd=os.getcwd()).load_gateway_security(overrides)

    run_server(
        host=host,
        port=port,
        grpc_port=grpc_port,
        password=configured.password,
        security_mode=configured.mode,
        password_hash=configured.password_hash,
        authorized_keys_path=configured.authorized_keys,
        jwt_secret=configured.jwt_secret,
        token_ttl_seconds=configured.token_ttl_seconds,
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
    from crabcode_core.types.config import LoggingSettings

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

    try:
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
    finally:
        store.close()


def entry() -> None:
    known_subcommands = {"main", "sessions", "stats", "gateway", "acp", "document-engine"}
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
