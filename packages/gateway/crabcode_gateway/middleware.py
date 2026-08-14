"""Gateway middleware — auth, logging, CORS, error handling.

Mirrors OpenCode's middleware.ts pattern.

NOTE: All custom middleware below are pure ASGI middleware (not
BaseHTTPMiddleware) because BaseHTTPMiddleware rejects WebSocket
upgrade requests with 403 by design.
"""

from __future__ import annotations

import json
import base64
import binascii
import hmac
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import ClientDisconnect

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


# ── Helper: check if scope is a WebSocket request ────────────────


def _is_websocket(scope: Scope) -> bool:
    return scope.get("type") == "websocket"


def _scope_header(scope: Scope, name: str) -> str:
    """Return the first value for an HTTP/WS header in an ASGI scope."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            try:
                return value.decode("latin-1")
            except (AttributeError, UnicodeDecodeError):
                return ""
    return ""


def _scope_query(scope: Scope, name: str) -> str | None:
    """Read one query parameter without constructing a Request/WebSocket."""
    raw = scope.get("query_string", b"")
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    values = parse_qs(raw, keep_blank_values=True).get(name)
    return values[0] if values else None


def is_authorized(scope: Scope, *, username: str, password: str | None) -> bool:
    """Validate gateway credentials for an HTTP or WebSocket ASGI scope.

    The gateway historically accepted a Basic password, a Bearer token, and
    the ``auth_token`` query parameter.  Keep all three forms so existing
    clients continue to work, while applying the same policy to WebSockets.
    """
    if not password:
        return True

    query_token = _scope_query(scope, "auth_token")
    if query_token is not None and hmac.compare_digest(query_token, password):
        return True

    auth_header = _scope_header(scope, "authorization")
    scheme, separator, credentials = auth_header.partition(" ")
    if not separator:
        return False

    if scheme.lower() == "bearer":
        return hmac.compare_digest(credentials, password)

    if scheme.lower() != "basic":
        return False

    try:
        decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
        user, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return hmac.compare_digest(user, username) and hmac.compare_digest(
        supplied_password, password
    )


# ── Auth middleware ──────────────────────────────────────────────


class AuthMiddleware:
    """Basic auth or bearer token authentication.

    Skipped if no password is configured.
    The same credentials are required for WebSocket upgrades.  Rejecting
    before ``websocket.accept`` avoids exposing an unauthenticated stream.
    """

    def __init__(self, app: ASGIApp, username: str = "crabcode", password: str | None = None) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_websocket(scope):
            if self.password and not is_authorized(
                scope,
                username=self.username,
                password=self.password,
            ):
                await send({
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "Unauthorized",
                })
                return
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Build a Request to inspect headers conveniently
        request = Request(scope, receive, send)

        # Skip auth for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if not self.password:
            await self.app(scope, receive, send)
            return

        if is_authorized(scope, username=self.username, password=self.password):
            await self.app(scope, receive, send)
            return

        # Not authenticated
        response = Response(
            content='{"detail":"Unauthorized"}',
            status_code=401,
            media_type="application/json",
            headers={"WWW-Authenticate": 'Basic realm="crabcode"'},
        )
        await response(scope, receive, send)


# ── Logging middleware ──────────────────────────────────────────


class LoggerMiddleware:
    """Log incoming requests and their duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_websocket(scope):
            # Log WebSocket connections briefly
            path = scope.get("path", "/")
            logger.info("WebSocket %s", path)
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        # Skip noisy endpoints
        if request.url.path in ("/health", "/event"):
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        logger.info("request %s %s", request.method, request.url.path)

        # Capture status code from the inner app's response
        status_code: int = 0

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        elapsed = time.monotonic() - start
        logger.info(
            "request %s %s → %d (%.3fs)",
            request.method,
            request.url.path,
            status_code,
            elapsed,
        )


# ── Error middleware ─────────────────────────────────────────────


class ErrorMiddleware:
    """Catch unhandled exceptions and return structured JSON errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_websocket(scope):
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            logger.exception("Unhandled error on %s", scope.get("path", "unknown"))
            response = Response(
                content=json.dumps({
                    "type": "error",
                    "message": str(exc),
                    "recoverable": False,
                    "error_type": "internal",
                }),
                status_code=500,
                media_type="application/json",
            )
            await response(scope, receive, send)


# ── CORS setup ──────────────────────────────────────────────────


def setup_cors(app: FastAPI, extra_origins: list[str] | None = None) -> None:
    """Add CORS middleware matching OpenCode's policy.

    Allows localhost, tauri, vscode-webview, and configurable origins.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
            r"|^https?://tauri\.localhost$"
            r"|^vscode-webview://"
        ),
        allow_origins=extra_origins or [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Helper: register all middleware ─────────────────────────────


def register_middleware(
    app: FastAPI,
    *,
    password: str | None = None,
    cors_origins: list[str] | None = None,
) -> None:
    """Register the full middleware stack on the FastAPI app."""
    # Order matters: outermost first
    app.add_middleware(ErrorMiddleware)

    if password:
        app.add_middleware(AuthMiddleware, password=password)

    app.add_middleware(LoggerMiddleware)

    setup_cors(app, extra_origins=cors_origins)
