"""Wire protocol metadata shared by CrabCode gateway transports."""

from __future__ import annotations


# Increment this value when clients and gateways need an explicit compatibility
# boundary. Additive changes may keep the same protocol version.
GATEWAY_PROTOCOL_VERSION = 1
GATEWAY_MIN_PROTOCOL_VERSION = 1
GATEWAY_MAX_PROTOCOL_VERSION = 1


__all__ = [
    "GATEWAY_MAX_PROTOCOL_VERSION",
    "GATEWAY_MIN_PROTOCOL_VERSION",
    "GATEWAY_PROTOCOL_VERSION",
]
