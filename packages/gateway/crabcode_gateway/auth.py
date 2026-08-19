"""Gateway authentication: password/public-key login and short-lived JWTs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any

import jwt

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
except ImportError:  # pragma: no cover - dependency is declared by gateway
    serialization = None  # type: ignore[assignment]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_jwt(payload: dict[str, Any], secret: str) -> str:
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_jwt(token: str, secret: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer="crabcode-gateway",
            audience="crabcode-api",
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError:
        return None


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), iterations)
        return hmac.compare_digest(_b64(actual), expected)
    except (ValueError, TypeError):
        return False


def _authorized_keys(path: str) -> dict[str, Any]:
    """Read an OpenSSH authorized_keys file and return id -> public key."""
    keys: dict[str, Any] = {}
    expanded = Path(path).expanduser()
    if serialization is None or not expanded.is_file():
        return keys
    try:
        lines = expanded.read_text(errors="replace").splitlines()
    except OSError:
        return keys
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        try:
            idx = next(
                i
                for i, value in enumerate(fields)
                if value.startswith(("ssh-", "ecdsa-", "sk-"))
            )
            key_type, encoded = fields[idx], fields[idx + 1]
            raw = base64.b64decode(encoded, validate=True)
            public_key = serialization.load_ssh_public_key(
                f"{key_type} {encoded}".encode()
            )
            fingerprint = "SHA256:" + _b64(hashlib.sha256(raw).digest())
            comment = " ".join(fields[idx + 2:]).strip()
            key_id = comment or fingerprint
            keys[key_id] = public_key
            keys.setdefault(fingerprint, public_key)
        except (StopIteration, ValueError, IndexError, TypeError):
            continue
    return keys


def verify_public_signature(path: str, key_id: str, message: bytes, signature_b64: str) -> bool:
    keys = _authorized_keys(path)
    public_key = keys.get(key_id)
    if public_key is None:
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature, message)
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            return False
        return True
    except Exception:
        return False


def new_token(*, subject: str, mode: str, secret: str, ttl: int) -> str:
    now = int(time.time())
    return make_jwt(
        {
            "iss": "crabcode-gateway",
            "aud": "crabcode-api",
            "sub": subject,
            "auth_mode": mode,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": secrets.token_hex(16),
        },
        secret,
    )
