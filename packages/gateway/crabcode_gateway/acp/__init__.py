"""ACP (Agent Client Protocol) layer for CrabCode Gateway.

Translates between the ACP JSON-RPC protocol (used by editors like
Zed and JetBrains) and CrabCode's internal Gateway REST API.
"""

from crabcode_gateway.acp.types import ACPConfig, ACPSessionState, ModelSelection

__all__ = [
    "ACPConfig",
    "ACPSessionState",
    "ModelSelection",
]
