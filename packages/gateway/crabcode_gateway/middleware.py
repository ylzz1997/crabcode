"""Gateway middleware — auth, logging, CORS, error handling.

Mirrors OpenCode's middleware.ts pattern.

NOTE: All custom middleware below are pure ASGI middleware (not
BaseHTTPMiddleware) because BaseHTTPMiddleware rejects WebSocket
upgrade requests with 403 by design.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import ClientDisconnect

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


# ── Helper: check if scope is a WebSocket request ────────────────


def _is_websocket(scope: Scope) -> bool:
    return scope.get("type") == "websocket"


# ── Auth middleware ──────────────────────────────────────────────


class AuthMiddleware:
    """Basic auth or bearer token authentication.

    Skipped if no password is configured.
    WebSocket requests are always passed through (auth is checked
    inside the WebSocket handler if needed).
    """

    def __init__(self, app: ASGIApp, username: str = "crabcode", password: str | None = None) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_websocket(scope):
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

        # Check auth_token query param → translate to Basic header
        auth_token = request.query_params.get("auth_token")
        if auth_token:
            import base64
            token = base64.b64encode(f"{self.username}:{auth_token}".encode()).decode()
            scope.setdefault("headers", [])
            headers = list(scope["headers"])
            headers.append((b"authorization", f"Basic {token}".encode()))
            scope["headers"] = headers

        auth_header = request.headers.get("authorization", "")

        # Support Bearer token (treat token value as the password)
        if auth_header.startswith("Bearer "):
            token_value = auth_header[7:]
            if token_value == self.password:
                await self.app(scope, receive, send)
                return

        if auth_header.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                user, pw = decoded.split(":", 1)
                if user == self.username and pw == self.password:
                    await self.app(scope, receive, send)
                    return
            except Exception:
                pass

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
