"""LSP client — connects to language servers via stdio and manages document sync + diagnostics.

Modeled after the OpenCode TypeScript LSP client.  Implements:
- Lifecycle: ``initialize`` request → ``initialized`` notification → ``shutdown`` → ``exit``
- Document sync: ``textDocument/didOpen`` / ``textDocument/didChange`` (incremental)
- Diagnostics: listens for ``textDocument/publishDiagnostics``
- Reverse requests: ``workspace/configuration``, ``client/registerCapability``
- File version tracking with per-URI counters
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

_JSONRPC_VERSION = "2.0"


class JsonRpcError(Exception):
    """Wraps a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# LSP Client
# ---------------------------------------------------------------------------


class LSPClient:
    """Async LSP client that communicates over stdio with a language server.

    Usage::

        client = LSPClient(command=["pyright-langserver", "--stdio"], root_uri=root_uri)
        await client.create()
        await client.touch_file(file_path)
        diag = await client.wait_for_diagnostics(file_path)
        await client.shutdown()
    """

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(
        self,
        command: list[str],
        root_uri: str,
        *,
        env: dict[str, str] | None = None,
        initialization_options: dict[str, Any] | None = None,
        trace: str | None = None,
    ) -> None:
        self._command = command
        self._root_uri = root_uri
        self._env = {**os.environ, **(env or {})}
        self._initialization_options = initialization_options
        self._trace = trace

        # Process + I/O
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._writer_lock = asyncio.Lock()

        # JSON-RPC state
        self._request_id: int = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # Document version tracking  {uri: version}
        self._file_versions: dict[str, int] = {}

        # Open documents  {uri: text}
        self._open_documents: dict[str, str] = {}

        # Diagnostics  {uri: list[dict]}
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}

        # Event fired when diagnostics arrive for a given URI
        self._diagnostic_events: dict[str, asyncio.Event] = {}

        # Reverse-request handlers
        self._registered_capabilities: dict[str, dict[str, Any]] = {}

        # Capabilities returned by the server
        self._server_capabilities: dict[str, Any] = {}

        # Shutdown flag
        self._shutting_down = False

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def create(self) -> dict[str, Any]:
        """Start the language server process, send ``initialize`` + ``initialized``.

        Returns the ``initialize`` result (server capabilities, etc.).
        """
        if self._process is not None:
            raise RuntimeError("LSP client already initialised")

        # Launch the server process
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )

        # Start the reader loop in the background
        self._reader_task = asyncio.create_task(self._reader_loop())

        # Also drain stderr for debugging
        asyncio.create_task(self._stderr_drain())

        # Send the initialize request
        init_params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "rootPath": _uri_to_path(self._root_uri),
            "capabilities": self._client_capabilities(),
            "trace": self._trace or "off",
        }
        if self._initialization_options is not None:
            init_params["initializationOptions"] = self._initialization_options

        result = await self._send_request("initialize", init_params)
        self._server_capabilities = result.get("capabilities", {})

        # Send the initialized notification (no params required by spec)
        await self._send_notification("initialized", {})

        logger.info(
            "LSP server initialised: %s", self._command[0],
        )
        return result

    async def touch_file(
        self,
        file_path: str | Path,
        text: str | None = None,
    ) -> None:
        """Open or update a file in the language server.

        If the file has not been opened yet, sends ``textDocument/didOpen``.
        Otherwise sends ``textDocument/didChange`` with the full text.
        """
        uri = _path_to_uri(str(file_path))

        if text is None:
            try:
                text = Path(file_path).read_text(encoding="utf-8")
            except OSError:
                logger.warning("Cannot read file for LSP sync: %s", file_path)
                return

        if uri in self._open_documents:
            # Increment version and send didChange
            version = self._file_versions.get(uri, 0) + 1
            self._file_versions[uri] = version
            self._open_documents[uri] = text

            await self._send_notification(
                "textDocument/didChange",
                {
                    "textDocument": {
                        "uri": uri,
                        "version": version,
                    },
                    "contentChanges": [
                        {"text": text},
                    ],
                },
            )
        else:
            # First open — didOpen
            version = 1
            self._file_versions[uri] = version
            self._open_documents[uri] = text

            # Determine language id from file extension
            language_id = _language_id(str(file_path))

            await self._send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": text,
                    },
                },
            )

    async def close_file(self, file_path: str | Path) -> None:
        """Send ``textDocument/didClose`` for a tracked file."""
        uri = _path_to_uri(str(file_path))
        if uri not in self._open_documents:
            return

        await self._send_notification(
            "textDocument/didClose",
            {
                "textDocument": {
                    "uri": uri,
                },
            },
        )
        self._open_documents.pop(uri, None)
        self._file_versions.pop(uri, None)

    async def wait_for_diagnostics(
        self,
        file_path: str | Path,
        *,
        timeout: float = 30.0,
        debounce: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Wait for diagnostics for *file_path*, with debounce.

        The LSP server may send multiple ``publishDiagnostics`` notifications
        in rapid succession (e.g. as it re-analyses).  We debounce by waiting
        *debounce* seconds after the **last** notification before returning.

        Returns the list of diagnostic dicts for the file.
        """
        uri = _path_to_uri(str(file_path))

        # If we already have diagnostics, return them immediately (caller can
        # still choose to wait again after touch_file).
        event = self._diagnostic_events.get(uri)
        if event is None:
            event = asyncio.Event()
            self._diagnostic_events[uri] = event

        # Wait for the first notification (or use existing data)
        if uri not in self.diagnostics:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for diagnostics: %s", uri)
                return self.diagnostics.get(uri, [])

        # Debounce: keep waiting as long as new notifications arrive within
        # the debounce window.
        while True:
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=debounce)
                # A new notification arrived — keep debouncing.
            except asyncio.TimeoutError:
                # No new notification within the debounce window.
                break

        return self.diagnostics.get(uri, [])

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Send an arbitrary LSP request and return the result.

        This is the public counterpart to ``_send_request`` so that
        :class:`LSPManager` (and other consumers) can issue requests like
        ``textDocument/hover``, ``textDocument/definition``, etc. without
        reaching into the private API.
        """
        return await self._send_request(method, params)

    async def shutdown(self) -> None:
        """Send ``shutdown`` request followed by ``exit`` notification and terminate."""
        if self._shutting_down:
            return
        self._shutting_down = True

        try:
            await self._send_request("shutdown", None)
        except (JsonRpcError, Exception):
            logger.debug("shutdown request failed (server may have exited)")

        try:
            await self._send_notification("exit", None)
        except Exception:
            logger.debug("exit notification failed")

        # Kill the process if still running
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()

        # Cancel the reader task
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        self._process = None
        self._reader_task = None
        logger.info("LSP server shut down: %s", self._command[0])

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def server_capabilities(self) -> dict[str, Any]:
        """Capabilities returned by the server in the ``initialize`` response."""
        return self._server_capabilities

    @property
    def file_versions(self) -> dict[str, int]:
        """Read-only view of current file version counters."""
        return dict(self._file_versions)

    @property
    def open_documents(self) -> dict[str, str]:
        """Read-only view of currently open document texts."""
        return dict(self._open_documents)

    # -----------------------------------------------------------------------
    # JSON-RPC transport
    # -----------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        self._request_id += 1
        request_id = self._request_id

        message: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        await self._write_message(message)

        try:
            result = await future
        finally:
            self._pending_requests.pop(request_id, None)

        return result

    async def _send_notification(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        message: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        await self._write_message(message)

    async def _send_response(
        self,
        request_id: int | str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC response (for reverse requests from the server)."""
        message: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
        }
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result

        await self._write_message(message)

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Serialise and write a JSON-RPC message over stdin (Content-Length header)."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("LSP server process not running")

        body = json.dumps(message, ensure_ascii=False)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"

        async with self._writer_lock:
            self._process.stdin.write(header.encode("utf-8") + body.encode("utf-8"))
            await self._process.stdin.drain()

        logger.debug("LSP → %s", message.get("method", f"response:{message.get('id')}"))

    # -----------------------------------------------------------------------
    # Reader loop
    # -----------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Background task: read JSON-RPC messages from the server stdout."""
        try:
            while self._process is not None and self._process.stdout is not None:
                message = await self._read_message()
                if message is None:
                    break
                asyncio.create_task(self._handle_message(message))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("LSP reader loop crashed")

    async def _read_message(self) -> dict[str, Any] | None:
        """Read a single JSON-RPC message (Content-Length header + body)."""
        if self._process is None or self._process.stdout is None:
            return None

        # Read headers until empty line
        content_length: int | None = None
        while True:
            line_bytes = await self._process.stdout.readline()
            if not line_bytes:
                return None  # EOF
            line = line_bytes.decode("utf-8").strip()
            if not line:
                break  # End of headers
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())

        if content_length is None:
            logger.warning("LSP message missing Content-Length header")
            return None

        body_bytes = await self._process.stdout.readexactly(content_length)
        return json.loads(body_bytes.decode("utf-8"))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Dispatch an incoming JSON-RPC message."""
        if "id" in message and "method" in message:
            # Reverse request from the server
            await self._handle_reverse_request(message)
        elif "id" in message:
            # Response to one of our requests
            self._handle_response(message)
        elif "method" in message:
            # Notification from the server
            await self._handle_notification(message)
        else:
            logger.warning("Unknown LSP message format: %s", message)

    def _handle_response(self, message: dict[str, Any]) -> None:
        """Resolve a pending request future with the server response."""
        request_id = message["id"]
        future = self._pending_requests.get(request_id)
        if future is None:
            logger.warning("LSP response for unknown request id: %s", request_id)
            return

        if "error" in message:
            error = message["error"]
            future.set_exception(
                JsonRpcError(
                    code=error.get("code", -1),
                    message=error.get("message", "Unknown error"),
                    data=error.get("data"),
                ),
            )
        else:
            future.set_result(message.get("result", {}))

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        """Handle a server-initiated notification."""
        method = message.get("method", "")
        params = message.get("params", {})

        logger.debug("LSP ← notification %s", method)

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diags = params.get("diagnostics", [])
            self.diagnostics[uri] = diags

            # Signal any waiters
            event = self._diagnostic_events.get(uri)
            if event is None:
                event = asyncio.Event()
                self._diagnostic_events[uri] = event
            event.set()

            logger.debug(
                "LSP diagnostics for %s: %d items", uri, len(diags),
            )
        elif method == "window/logMessage":
            log_type = params.get("type", 4)
            log_msg = params.get("message", "")
            _log_level = {1: logger.error, 2: logger.warning, 3: logger.info, 4: logger.debug}
            _log_level.get(log_type, logger.debug)("LSP server: %s", log_msg)
        elif method == "window/showMessage":
            logger.info("LSP server message: %s", params.get("message", ""))
        else:
            logger.debug("LSP unhandled notification: %s", method)

    async def _handle_reverse_request(self, message: dict[str, Any]) -> None:
        """Handle a server-initiated request (reverse request)."""
        method = message.get("method", "")
        request_id = message["id"]
        params = message.get("params", {})

        logger.debug("LSP ← reverse request %s (id=%s)", method, request_id)

        if method == "workspace/configuration":
            result = await self._handle_workspace_configuration(params)
            await self._send_response(request_id, result=result)
        elif method == "client/registerCapability":
            result = await self._handle_register_capability(params)
            await self._send_response(request_id, result=result)
        elif method == "client/unregisterCapability":
            result = await self._handle_unregister_capability(params)
            await self._send_response(request_id, result=result)
        else:
            logger.warning("LSP unhandled reverse request: %s", method)
            await self._send_response(
                request_id,
                error={
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            )

    # -----------------------------------------------------------------------
    # Reverse-request handlers
    # -----------------------------------------------------------------------

    async def _handle_workspace_configuration(
        self,
        params: dict[str, Any],
    ) -> list[Any]:
        """Respond to ``workspace/configuration`` requests.

        The server asks for configuration items identified by section names.
        We return an empty configuration for each requested section —
        subclasses or callers can override this to provide real settings.
        """
        items = params.get("items", [])
        results: list[Any] = []
        for item in items:
            section = item.get("section")
            # Return empty config by default; callers can subclass to override
            logger.debug("LSP workspace/configuration request for section: %s", section)
            results.append({})
        return results

    async def _handle_register_capability(
        self,
        params: dict[str, Any],
    ) -> None:
        """Handle ``client/registerCapability`` — store the registration."""
        registrations = params.get("registrations", [])
        for reg in registrations:
            reg_id = reg.get("id", str(uuid.uuid4()))
            method = reg.get("method", "")
            register_options = reg.get("registerOptions", {})
            self._registered_capabilities[reg_id] = {
                "method": method,
                "registerOptions": register_options,
            }
            logger.info("LSP registered capability: %s (%s)", method, reg_id)
        return None

    async def _handle_unregister_capability(
        self,
        params: dict[str, Any],
    ) -> None:
        """Handle ``client/unregisterCapability`` — remove the registration."""
        unregistrations = params.get("unregistrations", [])
        for unreg in unregistrations:
            reg_id = unreg.get("id", "")
            self._registered_capabilities.pop(reg_id, None)
            logger.info("LSP unregistered capability: %s", reg_id)
        return None

    # -----------------------------------------------------------------------
    # Client capabilities
    # -----------------------------------------------------------------------

    @staticmethod
    def _client_capabilities() -> dict[str, Any]:
        """Return the client capabilities dict sent in the ``initialize`` request."""
        return {
            "workspace": {
                "applyEdit": True,
                "configuration": True,
                "didChangeConfiguration": {
                    "dynamicRegistration": True,
                },
                "workspaceFolders": True,
                "symbol": {
                    "dynamicRegistration": True,
                },
            },
            "textDocument": {
                "synchronization": {
                    "dynamicRegistration": True,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                    "didSave": True,
                },
                "completion": {
                    "dynamicRegistration": True,
                    "completionItem": {
                        "snippetSupport": False,
                    },
                },
                "hover": {
                    "dynamicRegistration": True,
                },
                "publishDiagnostics": {
                    "relatedInformation": True,
                    "versionSupport": True,
                },
            },
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _stderr_drain(self) -> None:
        """Consume stderr from the server process and log it."""
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                logger.debug("LSP stderr: %s", line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _path_to_uri(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    resolved = Path(path).resolve()
    # Use as_posix to get forward slashes, then encode
    return resolved.as_uri()


def _uri_to_path(uri: str) -> str:
    """Convert a file:// URI back to a filesystem path."""
    if uri.startswith("file://"):
        # Handle both file:///path and file://host/path
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        return unquote(parsed.path)
    return uri


# Map of common file extensions to LSP language identifiers
_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".lua": "lua",
    ".r": "r",
    ".sql": "sql",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".md": "markdown",
    ".dart": "dart",
    ".elixir": "elixir",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".scala": "scala",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".hs": "haskell",
    ".ml": "fsharp",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".vim": "vim",
    ".zig": "zig",
    ".toml": "toml",
    ".ini": "ini",
    ".dockerfile": "dockerfile",
}


def _language_id(file_path: str) -> str:
    """Return the LSP language identifier for a file path."""
    suffix = Path(file_path).suffix.lower()
    if suffix in _LANGUAGE_MAP:
        return _LANGUAGE_MAP[suffix]
    # Special case: Dockerfile
    name = Path(file_path).name.lower()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return "dockerfile"
    return suffix.lstrip(".") if suffix else "plaintext"
