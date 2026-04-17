"""ACP stdio transport — wires CrabCodeACPAgent to stdin/stdout via ACP SDK.

Uses the official `acp.run_agent` convenience function which handles:
  - JSON-RPC framing over ndjson stdio streams
  - Request/response routing
  - Agent-side connection lifecycle
"""

from __future__ import annotations

import acp

from crabcode_core.logging_utils import get_logger
from crabcode_gateway.acp.agent import CrabCodeACPAgent
from crabcode_gateway.acp.types import ACPConfig

logger = get_logger(__name__)


async def run_acp_server(config: ACPConfig) -> None:
    """Start the ACP agent server on stdio.

    This function blocks until stdin is closed or a fatal error occurs.

    Usage::

        config = ACPConfig(base_url="http://127.0.0.1:4096")
        await run_acp_server(config)
    """
    agent = CrabCodeACPAgent(config)
    try:
        logger.info("acp_server_starting", extra={"base_url": config.base_url})
        await acp.run_agent(agent)
    except Exception:
        logger.exception("acp_server_error")
        raise
    finally:
        await agent.close()
        logger.info("acp_server_stopped")
