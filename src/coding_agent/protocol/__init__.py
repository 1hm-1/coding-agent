"""Versioned public process protocol for the coding-agent runtime."""

from coding_agent.protocol.headless import (
    PROTOCOL_VERSION,
    ProtocolError,
    load_execution_request,
    protocol_info,
    run_headless,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ProtocolError",
    "load_execution_request",
    "protocol_info",
    "run_headless",
]
