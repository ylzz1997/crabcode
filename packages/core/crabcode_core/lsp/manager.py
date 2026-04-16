"""LSP manager — main entry point for the LSP subsystem.

Responsibilities:
- Load config and merge built-in + custom LSP server definitions.
- Lazy-start clients via :meth:`get_clients` which matches file extensions,
  resolves the project root, spawns an :class:`LSPClient`, and caches it.
- Track broken servers (failed to start — skip retries).
- Track spawning clients (prevent concurrent starts for the same key).
- Expose ``touch_file``, ``diagnostics``, ``hover``, ``definition``,
  ``references`` methods that fan out to the appropriate clients.
- ``shutdown()`` to close all active clients.

Key invariants:
- Same ``(root_uri, server_id)`` pair gets only one client instance.
- One file may match multiple servers (e.g. ``.ts`` gets both *typescript*
  and *eslint*).
- :class:`LSPManager` is session-scoped (one per :class:`CoreSession`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.lsp.client import LSPClient, _path_to_uri
from crabcode_core.types.config import CrabCodeSettings, LspServerConfig

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Built-in LSP server definitions
# ---------------------------------------------------------------------------

BUILTIN_LSP_SERVERS: dict[str, LspServerConfig] = {
    "python": LspServerConfig(
        command=["pyright-langserver", "--stdio"],
        extensions=[".py", ".pyi", ".pyw"],
    ),
    "typescript": LspServerConfig(
        command=["typescript-language-server", "--stdio"],
        extensions=[".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"],
    ),
    "go": LspServerConfig(
        command=["gopls"],
        extensions=[".go"],
    ),
    "rust": LspServerConfig(
        command=["rust-analyzer"],
        extensions=[".rs"],
    ),
    "csharp": LspServerConfig(
        command=["omnisharp", "--stdio"],
        extensions=[".cs"],
    ),
    "java": LspServerConfig(
        command=["jdtls"],
        extensions=[".java"],
    ),
    "cpp": LspServerConfig(
        command=["clangd"],
        extensions=[".c", ".cpp", ".h", ".hpp", ".cc", ".cxx", ".hh", ".hxx"],
    ),
    "ruby": LspServerConfig(
        command=["solargraph", "stdio"],
        extensions=[".rb"],
    ),
    "php": LspServerConfig(
        command=["phpactor", "language-server"],
        extensions=[".php"],
    ),
    "elixir": LspServerConfig(
        command=["lexical"],
        extensions=[".ex", ".exs", ".elixir"],
    ),
    "dart": LspServerConfig(
        command=["dart", "language-server", "--protocol=lsp"],
        extensions=[".dart"],
    ),
    "lua": LspServerConfig(
        command=["lua-language-server"],
        extensions=[".lua"],
    ),
    "kotlin": LspServerConfig(
        command=["kotlin-language-server"],
        extensions=[".kt", ".kts"],
    ),
    "swift": LspServerConfig(
        command=["sourcekit-lsp"],
        extensions=[".swift"],
    ),
    "zig": LspServerConfig(
        command=["zls"],
        extensions=[".zig"],
    ),
    "scala": LspServerConfig(
        command=["metals"],
        extensions=[".scala"],
    ),
}


# ---------------------------------------------------------------------------
# Root-finding helpers
# ---------------------------------------------------------------------------

_ROOT_MARKERS: list[str] = [
    ".git",
    ".hg",
    ".svn",
    # Language-specific
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "*.sln",
    "*.csproj",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "mix.exs",
    "pubspec.yaml",
    "CMakeLists.txt",
    "Makefile",
]


def find_project_root(file_path: str | Path) -> str:
    """Walk upward from *file_path* to find the project root directory.

    The root is the nearest ancestor directory containing one of the
    conventional root markers (``.git``, ``pyproject.toml``, …).  Falls back
    to the dirname of *file_path* itself.
    """
    current = Path(file_path).resolve()
    if current.is_file():
        current = current.parent

    for _ in range(64):  # safety bound
        for marker in _ROOT_MARKERS:
            if marker.startswith("*"):
                # Glob-style marker — e.g. *.sln
                if any(current.glob(marker)):
                    return str(current)
            else:
                if (current / marker).exists():
                    return str(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    return str(current)


# ---------------------------------------------------------------------------
# LSPManager
# ---------------------------------------------------------------------------

# Type alias for the cache key: (root_uri, server_id)
_ClientKey = tuple[str, str]


class LSPManager:
    """Session-scoped manager for LSP client instances.

    Usage::

        manager = LSPManager(cwd="/path/to/project", settings=settings)
        clients = await manager.get_clients("src/main.py")
        for client in clients:
            await client.touch_file("src/main.py")
        diag = await manager.diagnostics("src/main.py")
        await manager.shutdown()
    """

    def __init__(
        self,
        cwd: str = ".",
        settings: CrabCodeSettings | None = None,
    ) -> None:
        self._cwd = os.path.abspath(cwd)
        self._settings = settings or CrabCodeSettings()

        # Merged server configs: {server_id: LspServerConfig}
        self._servers: dict[str, LspServerConfig] = {}
        self._load_servers()

        # Active client cache: {(root_uri, server_id): LSPClient}
        self._clients: dict[_ClientKey, LSPClient] = {}

        # Broken server set: {(root_uri, server_id)} — skip future starts
        self._broken: set[_ClientKey] = set()

        # Currently spawning: {(root_uri, server_id): asyncio.Task}
        # Prevents concurrent starts for the same key.
        self._spawning: dict[_ClientKey, asyncio.Task[LSPClient]] = {}

    # -------------------------------------------------------------------
    # Config loading
    # -------------------------------------------------------------------

    def _load_servers(self) -> None:
        """Merge built-in and user-configured LSP servers.

        User config can override built-in entries (same key = override).
        Setting ``disabled: true`` on a built-in server removes it.
        """
        merged: dict[str, LspServerConfig] = {}

        # 1. Start with built-in defaults
        for server_id, config in BUILTIN_LSP_SERVERS.items():
            merged[server_id] = config

        # 2. Merge user/custom from settings
        user_lsp = self._settings.lsp
        if isinstance(user_lsp, bool):
            # ``lsp: false`` → disable all built-in servers
            if not user_lsp:
                merged.clear()
        elif isinstance(user_lsp, dict):
            for server_id, config in user_lsp.items():
                if config.disabled and server_id in merged:
                    # Explicitly disable a built-in server
                    del merged[server_id]
                    continue
                if not config.command and server_id in merged:
                    # No command provided — keep built-in unless disabled
                    continue
                merged[server_id] = config

        # Remove any entries with empty command
        self._servers = {
            sid: cfg for sid, cfg in merged.items() if cfg.command
        }

    # -------------------------------------------------------------------
    # Extension matching
    # -------------------------------------------------------------------

    def _matching_servers(self, file_path: str | Path) -> list[str]:
        """Return server IDs whose ``extensions`` list matches *file_path*."""
        suffix = Path(file_path).suffix.lower()
        if not suffix:
            return []

        matched: list[str] = []
        for server_id, config in self._servers.items():
            if suffix in config.extensions:
                matched.append(server_id)
        return matched

    # -------------------------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------------------------

    async def get_clients(self, file_path: str | Path) -> list[LSPClient]:
        """Return all LSP clients relevant to *file_path*.

        For each matching server:
        1. Compute the project root.
        2. Build the cache key ``(root_uri, server_id)``.
        3. If a client is cached, return it.
        4. If the key is broken, skip it.
        5. If a spawn is in progress, await it.
        6. Otherwise, start a new client and cache it.
        """
        file_path = str(file_path)
        matching = self._matching_servers(file_path)
        if not matching:
            return []

        root = find_project_root(file_path)
        root_uri = _path_to_uri(root)

        clients: list[LSPClient] = []
        tasks: list[tuple[_ClientKey, asyncio.Task[LSPClient]]] = []

        for server_id in matching:
            key = (root_uri, server_id)

            # Already running
            if key in self._clients:
                clients.append(self._clients[key])
                continue

            # Previously broken — skip
            if key in self._broken:
                continue

            # Currently spawning — wait for it
            if key in self._spawning:
                tasks.append((key, self._spawning[key]))
                continue

            # Start a new client
            config = self._servers[server_id]

            async def _start(
                cfg: LspServerConfig = config,
                k: _ClientKey = key,
                sid: str = server_id,
                ru: str = root_uri,
            ) -> LSPClient:
                return await self._start_client(cfg, k, sid, ru)

            task = asyncio.create_task(_start())
            self._spawning[key] = task
            tasks.append((key, task))

        # Await all pending spawns
        for key, task in tasks:
            try:
                client = await task
                clients.append(client)
            except Exception:
                logger.warning(
                    "LSP client failed to start for %s at %s",
                    key[1], key[0],
                    exc_info=True,
                )

        return clients

    async def _start_client(
        self,
        config: LspServerConfig,
        key: _ClientKey,
        server_id: str,
        root_uri: str,
    ) -> LSPClient:
        """Start a single LSP client, update caches, and return it."""
        try:
            client = LSPClient(
                command=config.command,
                root_uri=root_uri,
                env=config.env or None,
                initialization_options=config.initialization or None,
            )
            await client.create()
            self._clients[key] = client
            logger.info(
                "LSP client started: %s (root=%s)",
                server_id, root_uri,
            )
            return client
        except Exception:
            self._broken.add(key)
            raise
        finally:
            self._spawning.pop(key, None)

    # -------------------------------------------------------------------
    # Convenience methods (fan-out to all matching clients)
    # -------------------------------------------------------------------

    async def touch_file(
        self,
        file_path: str | Path,
        text: str | None = None,
    ) -> None:
        """Open or update *file_path* in every matching LSP client."""
        clients = await self.get_clients(file_path)
        for client in clients:
            try:
                await client.touch_file(file_path, text=text)
            except Exception:
                logger.warning(
                    "LSP touch_file failed for %s", file_path,
                    exc_info=True,
                )

    async def diagnostics(
        self,
        file_path: str | Path,
        *,
        timeout: float = 30.0,
        debounce: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Collect diagnostics for *file_path* from all matching clients.

        Returns a merged list of diagnostic dicts.  Each dict includes an
        extra ``"source"`` key indicating the server that produced it.
        """
        clients = await self.get_clients(file_path)
        all_diags: list[dict[str, Any]] = []

        for client in clients:
            try:
                diags = await client.wait_for_diagnostics(
                    file_path, timeout=timeout, debounce=debounce,
                )
                for d in diags:
                    all_diags.append({**d, "source": _client_source(client)})
            except Exception:
                logger.warning(
                    "LSP diagnostics failed for %s", file_path,
                    exc_info=True,
                )

        return all_diags

    async def hover(
        self,
        file_path: str | Path,
        line: int,
        character: int,
    ) -> list[dict[str, Any]]:
        """Request hover information at (*line*, *character*) from matching clients.

        *line* and *character* are 0-based as per the LSP spec.

        Returns a list of hover results (one per server), each with an extra
        ``"source"`` key.
        """
        clients = await self.get_clients(file_path)
        results: list[dict[str, Any]] = []

        uri = _path_to_uri(str(file_path))
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }

        for client in clients:
            try:
                result = await client.send_request("textDocument/hover", params)
                if result:
                    results.append({**result, "source": _client_source(client)})
            except Exception:
                logger.debug(
                    "LSP hover failed for %s at %d:%d",
                    file_path, line, character,
                    exc_info=True,
                )

        return results

    async def definition(
        self,
        file_path: str | Path,
        line: int,
        character: int,
    ) -> list[dict[str, Any]]:
        """Request go-to-definition at (*line*, *character*) from matching clients.

        *line* and *character* are 0-based.

        Returns a list of location / location-link results with ``"source"``.
        """
        clients = await self.get_clients(file_path)
        results: list[dict[str, Any]] = []

        uri = _path_to_uri(str(file_path))
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }

        for client in clients:
            try:
                result = await client.send_request(
                    "textDocument/definition", params,
                )
                if result:
                    results.append({**result, "source": _client_source(client)})
            except Exception:
                logger.debug(
                    "LSP definition failed for %s at %d:%d",
                    file_path, line, character,
                    exc_info=True,
                )

        return results

    async def references(
        self,
        file_path: str | Path,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list[dict[str, Any]]:
        """Request find-references at (*line*, *character*) from matching clients.

        *line* and *character* are 0-based.

        Returns a list of reference location results with ``"source"``.
        """
        clients = await self.get_clients(file_path)
        results: list[dict[str, Any]] = []

        uri = _path_to_uri(str(file_path))
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }

        for client in clients:
            try:
                result = await client.send_request(
                    "textDocument/references", params,
                )
                if result:
                    results.append({**result, "source": _client_source(client)})
            except Exception:
                logger.debug(
                    "LSP references failed for %s at %d:%d",
                    file_path, line, character,
                    exc_info=True,
                )

        return results

    # -------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close all active LSP clients and clear caches."""
        # Cancel any in-progress spawns
        for key, task in self._spawning.items():
            task.cancel()
        self._spawning.clear()

        # Shut down active clients
        for key, client in self._clients.items():
            try:
                await client.shutdown()
            except Exception:
                logger.warning(
                    "LSP client shutdown failed for %s at %s",
                    key[1], key[0],
                    exc_info=True,
                )

        self._clients.clear()
        self._broken.clear()
        logger.info("LSP manager shut down")

    # -------------------------------------------------------------------
    # Introspection
    # -------------------------------------------------------------------

    @property
    def servers(self) -> dict[str, LspServerConfig]:
        """Read-only view of the merged server configurations."""
        return dict(self._servers)

    @property
    def active_clients(self) -> dict[str, LSPClient]:
        """Active clients keyed by ``"root_uri|server_id"``."""
        return {
            f"{root_uri}|{server_id}": client
            for (root_uri, server_id), client in self._clients.items()
        }

    @property
    def broken_servers(self) -> set[str]:
        """Set of ``"root_uri|server_id"`` strings for broken servers."""
        return {f"{root_uri}|{server_id}" for root_uri, server_id in self._broken}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_source(client: LSPClient) -> str:
    """Derive a human-readable source label from a client's command."""
    if client._command:
        return Path(client._command[0]).name
    return "unknown"
