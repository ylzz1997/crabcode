"""Same-machine transport for messages between independent CrabCode sessions.

Each live session publishes a small registry record and listens on a Unix
domain socket.  Registry files provide discovery; the socket is the delivery
and acknowledgement boundary.  Message contents never include transcripts or
files and are always tagged with their originating session.
"""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json
import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from crabcode_core.logging_utils import get_logger

logger = get_logger(__name__)

_PROTOCOL_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in cleaned.split("-") if part)[:48] or "session"


class PeerRecord(BaseModel):
    """Discoverable identity for one live CrabCode session."""

    version: int = _PROTOCOL_VERSION
    session_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    pid: int = Field(gt=0)
    socket_path: str = Field(min_length=1)
    auth_token: str = Field(min_length=32, repr=False)
    permission_class: Literal["prompting", "bypass"] = "prompting"
    started_at: str = Field(default_factory=_now_iso)


class PeerMessage(BaseModel):
    """Plain-text envelope delivered from one session to another."""

    version: int = _PROTOCOL_VERSION
    type: Literal["message"] = "message"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_session_id: str = Field(min_length=1)
    from_name: str = Field(min_length=1)
    from_cwd: str = Field(min_length=1)
    sender_permission_class: Literal["prompting", "bypass"] = "prompting"
    to_session_id: str = Field(min_length=1)
    to_auth_token: str = Field(min_length=32, repr=False)
    text: str = Field(min_length=1)
    sent_at: str = Field(default_factory=_now_iso)


class PeerDelivery(BaseModel):
    """Acknowledgement returned by the receiving session."""

    message_id: str
    status: Literal["delivered", "held", "refused", "failed"]
    detail: str = ""


InboundHandler = Callable[[PeerMessage], Awaitable[bool]]
HoldHandler = Callable[[PeerMessage], Awaitable[PeerDelivery]]
PermissionClassProvider = Callable[[], Literal["prompting", "bypass"]]


class PeerRuntime:
    """Publish, discover, and message independent local sessions."""

    def __init__(
        self,
        *,
        session_id: str,
        cwd: str,
        on_message: InboundHandler,
        on_hold: HoldHandler | None,
        permission_class_provider: PermissionClassProvider,
        name: str | None = None,
        inbound: Literal["auto", "accept", "hold", "refuse"] = "auto",
        registry_root: Path | None = None,
        max_message_size_bytes: int = 10_000,
        connect_timeout_seconds: float = 3.0,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        if inbound not in {"auto", "accept", "hold", "refuse"}:
            raise ValueError(f"Unsupported inbound policy: {inbound}")
        if max_message_size_bytes <= 0:
            raise ValueError("max_message_size_bytes must be greater than zero")

        self.session_id = session_id
        self.cwd = os.path.abspath(cwd)
        self.name = name or f"{_safe_slug(Path(self.cwd).name)}-{session_id[:6]}"
        self.inbound = inbound
        self._on_message = on_message
        self._on_hold = on_hold
        self._permission_class_provider = permission_class_provider
        self._registry_root = registry_root or Path.home() / ".crabcode" / "peers"
        self._max_message_size_bytes = max_message_size_bytes
        self._connect_timeout = connect_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._record: PeerRecord | None = None
        self._registry_path = self._registry_root / (
            hashlib.sha256(session_id.encode()).hexdigest()[:24] + ".json"
        )
        # Darwin limits AF_UNIX paths to roughly 104 bytes. Configured roots
        # (and pytest's per-test directories) can easily exceed that, so keep
        # discovery records there but fall back to a short, user-scoped socket
        # directory when necessary.
        socket_root = self._registry_root
        socket_probe = socket_root / ("x" * 24 + ".sock")
        if len(os.fsencode(socket_probe)) >= 100:
            uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
            temp_root = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
            socket_root = temp_root / f"crabcode-peers-{uid}"
        self._socket_root = socket_root
        socket_key = f"{session_id}:{os.getpid()}:{uuid.uuid4()}"
        self._socket_path = self._socket_root / (
            hashlib.sha256(socket_key.encode()).hexdigest()[:24] + ".sock"
        )
        self._auth_token = secrets.token_urlsafe(32)
        self._close_lock = asyncio.Lock()
        self._seen_message_ids: dict[str, float] = {}
        self._recent_payloads: dict[tuple[str, str], float] = {}
        self._sender_windows: dict[str, deque[float]] = {}

    @property
    def record(self) -> PeerRecord | None:
        return self._record.model_copy(deep=True) if self._record else None

    async def start(self) -> None:
        """Bind the inbox socket and atomically publish this session."""
        if self._server is not None:
            return
        if not hasattr(asyncio, "start_unix_server"):
            raise RuntimeError("Cross-session messaging requires Unix domain sockets")

        self._registry_root.mkdir(parents=True, exist_ok=True)
        self._socket_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._registry_root, 0o700)
            os.chmod(self._socket_root, 0o700)
        except OSError:
            logger.debug("Could not restrict peer registry permissions", exc_info=True)

        try:
            self._socket_path.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self._socket_path),
                limit=self._max_message_size_bytes + 16_384,
            )
            os.chmod(self._socket_path, 0o600)
            record = PeerRecord(
                session_id=self.session_id,
                name=self.name,
                cwd=self.cwd,
                pid=os.getpid(),
                socket_path=str(self._socket_path),
                auth_token=self._auth_token,
                permission_class=self._permission_class_provider(),
            )
            self._write_record(record)
            self._record = record
        except BaseException:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            self._socket_path.unlink(missing_ok=True)
            raise

    async def close(self) -> None:
        """Stop accepting messages and remove this runtime's registry entry."""
        async with self._close_lock:
            server = self._server
            self._server = None
            if server is not None:
                server.close()
                await server.wait_closed()
            self._remove_own_record()
            self._socket_path.unlink(missing_ok=True)
            self._record = None

    def close_nowait(self) -> None:
        """Synchronously make a peer undiscoverable during a session switch."""
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        self._remove_own_record()
        self._socket_path.unlink(missing_ok=True)
        self._record = None

    def list_peers(self) -> list[PeerRecord]:
        """Return live discoverable peers, pruning records from dead processes."""
        try:
            paths = list(self._registry_root.glob("*.json"))
        except OSError:
            return []

        peers: list[PeerRecord] = []
        for path in paths:
            try:
                record = PeerRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("Ignoring malformed peer record %s", path, exc_info=True)
                continue
            if record.session_id == self.session_id:
                continue
            if not self._record_is_live(record):
                self._remove_stale_record(path, record)
                continue
            peers.append(record)
        return sorted(peers, key=lambda peer: (peer.name, peer.session_id))

    async def send(self, target: str, text: str) -> PeerDelivery:
        """Resolve *target*, deliver plain text, and wait for an acknowledgement."""
        if not isinstance(text, str) or not text.strip():
            return PeerDelivery(message_id="", status="failed", detail="Message text is empty")
        if len(text.encode("utf-8")) > self._max_message_size_bytes:
            return PeerDelivery(
                message_id="",
                status="failed",
                detail=f"Message exceeds {self._max_message_size_bytes} bytes",
            )

        try:
            peer = self.resolve_peer(target)
        except ValueError as exc:
            return PeerDelivery(message_id="", status="failed", detail=str(exc))

        message = PeerMessage(
            from_session_id=self.session_id,
            from_name=self.name,
            from_cwd=self.cwd,
            sender_permission_class=self._permission_class_provider(),
            to_session_id=peer.session_id,
            to_auth_token=peer.auth_token,
            text=text,
        )
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(peer.socket_path),
                timeout=self._connect_timeout,
            )
            assert writer is not None
            writer.write(message.model_dump_json().encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=self._connect_timeout)
            raw = await asyncio.wait_for(reader.readline(), timeout=self._connect_timeout)
            if not raw:
                raise ConnectionError("Recipient closed without an acknowledgement")
            payload = json.loads(raw)
            return PeerDelivery(
                message_id=message.id,
                status=payload.get("status", "failed"),
                detail=str(payload.get("detail", "")),
            )
        except Exception as exc:
            self._remove_record_if_matches(peer)
            return PeerDelivery(
                message_id=message.id,
                status="failed",
                detail=f"Could not reach {peer.name}: {exc}",
            )
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass

    def resolve_peer(self, target: str) -> PeerRecord:
        """Resolve a unique peer by exact name, session ID, or ID prefix."""
        selector = str(target or "").strip()
        if not selector:
            raise ValueError("Target session is required")
        peers = self.list_peers()
        exact_id = [peer for peer in peers if peer.session_id == selector]
        if exact_id:
            return exact_id[0]
        exact_name = [peer for peer in peers if peer.name == selector]
        if len(exact_name) == 1:
            return exact_name[0]
        if len(exact_name) > 1:
            raise ValueError(f"Session name '{selector}' is ambiguous; use a session ID")
        prefix = [peer for peer in peers if peer.session_id.startswith(selector)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise ValueError(f"Session ID prefix '{selector}' is ambiguous")
        raise ValueError(f"No live session matches '{selector}'")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, str] = {"status": "failed", "detail": "Invalid message"}
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=self._connect_timeout)
            if not raw or len(raw) > self._max_message_size_bytes + 16_384:
                raise ValueError("Message frame is empty or too large")
            payload = json.loads(raw)
            message = PeerMessage.model_validate(payload)
            if message.version != _PROTOCOL_VERSION:
                raise ValueError(f"Unsupported peer protocol version {message.version}")
            if message.to_session_id != self.session_id:
                raise ValueError("Message was addressed to a different session")
            if not secrets.compare_digest(message.to_auth_token, self._auth_token):
                raise ValueError("Invalid inbox authentication token")
            if len(message.text.encode("utf-8")) > self._max_message_size_bytes:
                raise ValueError("Message text exceeds the configured byte limit")
            throttle_reason = self._throttle_reason(message)
            if throttle_reason:
                response = {"status": "refused", "detail": throttle_reason}
            else:
                decision = self._inbound_decision(message.sender_permission_class)
                if decision == "refuse":
                    response = {
                        "status": "refused",
                        "detail": "Receiving session's inbound policy refused the message",
                    }
                elif decision == "hold":
                    if self._on_hold is None:
                        response = {
                            "status": "refused",
                            "detail": "Receiving session cannot request inbound approval",
                        }
                    else:
                        delivery = await self._on_hold(message)
                        response = {
                            "status": delivery.status,
                            "detail": delivery.detail,
                        }
                elif await self._on_message(message):
                    response = {"status": "delivered", "detail": ""}
                else:
                    response = {"status": "failed", "detail": "Receiving queue is unavailable"}
        except Exception as exc:
            response = {"status": "failed", "detail": str(exc)}
        finally:
            try:
                writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    def _inbound_decision(
        self,
        sender_class: Literal["prompting", "bypass"],
    ) -> Literal["accept", "hold", "refuse"]:
        if self.inbound == "accept":
            return "accept"
        if self.inbound == "hold":
            return "hold"
        if self.inbound == "refuse":
            return "refuse"
        return (
            "accept"
            if sender_class == self._permission_class_provider()
            else "hold"
        )

    def _throttle_reason(self, message: PeerMessage) -> str:
        """Bound loops and identical repeats before they reach the model queue."""
        now = asyncio.get_running_loop().time()
        cutoff = now - 60.0
        self._seen_message_ids = {
            message_id: seen_at
            for message_id, seen_at in self._seen_message_ids.items()
            if seen_at >= cutoff
        }
        self._recent_payloads = {
            key: seen_at
            for key, seen_at in self._recent_payloads.items()
            if seen_at >= now - 2.0
        }
        if message.id in self._seen_message_ids:
            return "Duplicate message ID"

        window = self._sender_windows.setdefault(message.from_session_id, deque())
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= 30:
            return "Sender rate limit exceeded"

        payload_key = (message.from_session_id, message.text)
        if payload_key in self._recent_payloads:
            return "Identical repeated message"

        self._seen_message_ids[message.id] = now
        self._recent_payloads[payload_key] = now
        window.append(now)
        return ""

    def _write_record(self, record: PeerRecord) -> None:
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._registry_root,
                prefix=f".{self._registry_path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                handle.write(record.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self._registry_path)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _remove_own_record(self) -> None:
        try:
            current = PeerRecord.model_validate_json(
                self._registry_path.read_text(encoding="utf-8")
            )
        except Exception:
            return
        if current.socket_path == str(self._socket_path):
            self._registry_path.unlink(missing_ok=True)

    @staticmethod
    def _record_is_live(record: PeerRecord) -> bool:
        if not Path(record.socket_path).exists():
            return False
        try:
            os.kill(record.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _remove_stale_record(self, path: Path, record: PeerRecord) -> None:
        try:
            path.unlink(missing_ok=True)
            socket_path = Path(record.socket_path)
            allowed_roots = {
                self._registry_root.resolve(),
                self._socket_root.resolve(),
            }
            if socket_path.parent.resolve() in allowed_roots:
                socket_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not prune stale peer %s", record.session_id, exc_info=True)

    def _remove_record_if_matches(self, peer: PeerRecord) -> None:
        path = self._registry_root / (
            hashlib.sha256(peer.session_id.encode()).hexdigest()[:24] + ".json"
        )
        try:
            current = PeerRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if current.socket_path == peer.socket_path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
