"""Authentication endpoints used to bootstrap short-lived gateway JWTs."""

from __future__ import annotations

import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from crabcode_gateway.auth import new_token, verify_public_signature

router = APIRouter(prefix="/auth", tags=["auth"])

_PASSWORD_WINDOW_SECONDS = 60
_PASSWORD_MAX_FAILURES = 5


class PasswordTokenRequest(BaseModel):
    grant_type: Literal["password"]
    password: str = Field(min_length=1)


class PublicKeyTokenRequest(BaseModel):
    grant_type: Literal["publickey"]
    key_id: str = Field(min_length=1)
    challenge: str = Field(min_length=1)
    signature: str = Field(min_length=1)


def _password_client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_password_rate_limit(request: Request) -> tuple[str, list[float]]:
    client = _password_client(request)
    cutoff = time.monotonic() - _PASSWORD_WINDOW_SECONDS
    failures = [
        value
        for value in request.app.state.auth_failures.get(client, [])
        if value >= cutoff
    ]
    request.app.state.auth_failures[client] = failures
    if len(failures) >= _PASSWORD_MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many authentication failures; retry later",
            headers={"Retry-After": str(_PASSWORD_WINDOW_SECONDS)},
        )
    return client, failures


@router.get("/challenge")
async def challenge(request: Request) -> dict[str, str | int]:
    security = request.app.state.gateway_security
    if security.mode not in ("publickey", "mixed"):
        raise HTTPException(
            status_code=404,
            detail="Public-key authentication is disabled",
        )
    nonce = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + 60
    now = int(time.time())
    challenges = request.app.state.auth_challenges
    for value, expiry in list(challenges.items()):
        if expiry < now:
            challenges.pop(value, None)
    if len(challenges) >= 10_000:
        raise HTTPException(status_code=503, detail="Too many pending challenges")
    challenges[nonce] = expires_at
    signing_payload = f"crabcode-gateway:{nonce}:{expires_at}"
    return {
        "challenge": nonce,
        "expires_at": expires_at,
        "signing_payload": signing_payload,
    }


@router.get("/info")
async def info(request: Request) -> dict[str, object]:
    security = request.app.state.gateway_security
    methods: list[str] = []
    if security.mode in ("password", "mixed"):
        methods.append("password")
    if security.mode in ("publickey", "mixed"):
        methods.append("publickey")
    return {
        "mode": security.mode,
        "methods": methods,
        "token_ttl_seconds": security.token_ttl_seconds,
    }


@router.post("/token")
async def token(
    request: Request,
    body: Annotated[
        PasswordTokenRequest | PublicKeyTokenRequest,
        Field(discriminator="grant_type"),
    ],
):
    security = request.app.state.gateway_security
    secret = request.app.state.gateway_jwt_secret
    if isinstance(body, PasswordTokenRequest):
        client, failures = _check_password_rate_limit(request)
        if security.mode not in (
            "password",
            "mixed",
        ) or not request.app.state.verify_gateway_password(body.password):
            failures.append(time.monotonic())
            raise HTTPException(status_code=401, detail="Invalid password")
        request.app.state.auth_failures.pop(client, None)
        return {
            "access_token": new_token(
                subject="password",
                mode="password",
                secret=secret,
                ttl=security.token_ttl_seconds,
            ),
            "token_type": "bearer",
            "expires_in": security.token_ttl_seconds,
        }

    if security.mode not in ("publickey", "mixed"):
        raise HTTPException(
            status_code=404,
            detail="Public-key authentication is disabled",
        )
    expires_at = request.app.state.auth_challenges.pop(body.challenge, None)
    if not expires_at or expires_at < int(time.time()):
        raise HTTPException(status_code=401, detail="Challenge expired or already used")
    message = f"crabcode-gateway:{body.challenge}:{expires_at}".encode()
    if not verify_public_signature(
        security.authorized_keys,
        body.key_id,
        message,
        body.signature,
    ):
        raise HTTPException(status_code=401, detail="Invalid public-key signature")
    return {
        "access_token": new_token(
            subject=body.key_id,
            mode="publickey",
            secret=secret,
            ttl=security.token_ttl_seconds,
        ),
        "token_type": "bearer",
        "expires_in": security.token_ttl_seconds,
    }
