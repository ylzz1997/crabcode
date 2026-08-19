"""CrabCode Gateway - multi-protocol server for CrabCode."""

from crabcode_core import VERSION
from crabcode_gateway.protocol import (
    GATEWAY_MAX_PROTOCOL_VERSION,
    GATEWAY_MIN_PROTOCOL_VERSION,
    GATEWAY_PROTOCOL_VERSION,
)

__version__ = VERSION

__all__ = [
    "GATEWAY_MAX_PROTOCOL_VERSION",
    "GATEWAY_MIN_PROTOCOL_VERSION",
    "GATEWAY_PROTOCOL_VERSION",
    "VERSION",
    "__version__",
]
