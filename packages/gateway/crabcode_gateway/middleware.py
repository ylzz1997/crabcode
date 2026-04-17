"""Gateway middleware — auth, logging, CORS, error handling.

Mirrors OpenCode's middleware.ts pattern.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)


# ── Auth middleware ──────────────────────────────────────────────


class AuthMiddleware(BaseHTTPMiddleware):
    """Basic auth or bearer token authentication.

    Skipped if no password is configured.
    """

    def __init__(self, app: Any, username: str = "crabcode", password: str | None = None) -> None:
        super().__init__(app)
        self.username = username
        self.password = password

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip auth for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        if not self.password:
            return await call_next(request)

        # Check auth_token query param → translate to Basic header
        auth_token = request.query_params.get("auth_token")
        if auth_token:
            import base64
            token = base64.b64encode(f"{self.username}:{auth_token}".encode()).decode()
            # Mutate scope headers (Starlette internal detail)
            request.scope["headers"].append(
                (b"authorization", f"Basic {token}".encode())
            )

        auth_header = request.headers.get("authorization", "")

        if auth_header.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                user, pw = decoded.split(":", 1)
                if user == self.username and pw == self.password:
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            content='{"detail":"Unauthorized"}',
            status_code=401,
            media_type="application/json",
            headers={"WWW-Authenticate": 'Basic realm="crabcode"'},
        )


# ── Logging middleware ──────────────────────────────────────────


class LoggerMiddleware(BaseHTTPMiddleware):
    """Log incoming requests and their duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip noisy endpoints
        if request.url.path in ("/health", "/event"):
            return await call_next(request)

        start = time.monotonic()
        logger.info("request %s %s", request.method, request.url.path)

        response = await call_next(request)

        elapsed = time.monotonic() - start
        logger.info(
            "request %s %s → %d (%.3fs)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response


# ── Error middleware ─────────────────────────────────────────────


class ErrorMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions and return structured JSON errors."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            import json
            return Response(
                content=json.dumps({
                    "type": "error",
                    "message": str(exc),
                    "recoverable": False,
                    "error_type": "internal",
                }),
                status_code=500,
                media_type="application/json",
            )


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
