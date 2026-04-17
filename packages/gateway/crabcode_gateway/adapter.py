"""Protocol adapter abstraction — inspired by OpenCode's adapter.ts.

Each concrete adapter (HTTP, gRPC) implements the ProtocolAdapter ABC,
allowing the server to start/stop multiple protocols uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ProtocolAdapter(ABC):
    """Abstract base class for protocol adapters.

    Mirrors OpenCode's Adapter interface which abstracts away the
    runtime-specific server creation (Bun vs Node).
    """

    @abstractmethod
    async def start(self, host: str, port: int) -> None:
        """Start serving on the given host:port."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the server."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Whether the server is currently running."""
