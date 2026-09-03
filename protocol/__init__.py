"""
protocol — the wire format shared by the Bella server and every Bella client.

Deliberately dependency-free (stdlib only) and self-contained so a Windows or
browser-adjacent client can vendor just this package without pulling in the
`bella` runtime, Ollama, or any macOS code.

  protocol.models   Envelope + payload dataclasses, encode/decode
  protocol.events   the (small, explicit) set of valid event `type` strings
"""

from protocol.events import (
    CLIENT_EVENTS,
    SERVER_EVENTS,
    C,
    S,
    is_client_event,
    is_server_event,
)
from protocol.models import (
    PROTOCOL_VERSION,
    DeviceInfo,
    Envelope,
    ToolResult,
    decode,
    encode,
    new_request_id,
    now_ts,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "DeviceInfo",
    "ToolResult",
    "encode",
    "decode",
    "new_request_id",
    "now_ts",
    "C",
    "S",
    "CLIENT_EVENTS",
    "SERVER_EVENTS",
    "is_client_event",
    "is_server_event",
]
