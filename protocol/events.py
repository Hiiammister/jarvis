"""
protocol.events — the complete, explicit set of event types.

Small on purpose. Anything not in these sets is rejected by the server before
it reaches any handler (see server.websocket).
"""

from __future__ import annotations


class C:
    """client -> server event types."""

    HELLO = "hello"                       # register: DeviceInfo in payload
    MESSAGE = "message"                   # a user turn: {"text": ...}
    TOOL_RESULT = "tool_result"           # answer to a device_tool_request
    DEVICE_STATUS = "device_status"       # {"state": "active"|"idle"|...}
    DEVICE_CAPABILITIES = "device_capabilities"  # updated capability list
    HEARTBEAT = "heartbeat"
    DISCONNECT = "disconnect"


class S:
    """server -> client event types."""

    CONNECTED = "connected"               # ack of hello: {"device_id", "server", ...}
    ASSISTANT_START = "assistant_start"
    ASSISTANT_CHUNK = "assistant_chunk"   # {"text": ...}
    ASSISTANT_END = "assistant_end"       # {"text": full, "route": ..., "reply": ...}
    THINKING = "thinking"                 # {"label": "thinking"|...}
    TOOL_START = "tool_start"             # {"tool", "title", "args"?}
    TOOL_END = "tool_end"                 # {"tool", "ok", "summary", "elapsed"}
    DEVICE_TOOL_REQUEST = "device_tool_request"  # {"tool", "args", "timeout"}
    NOTIFICATION = "notification"         # {"title", "body"}
    ERROR = "error"                       # {"message", "code"?}
    PING = "ping"
    DEVICES = "devices"                   # {"devices": [...]}  (push on change)


CLIENT_EVENTS: frozenset[str] = frozenset(
    v for k, v in vars(C).items() if not k.startswith("_") and isinstance(v, str)
)
SERVER_EVENTS: frozenset[str] = frozenset(
    v for k, v in vars(S).items() if not k.startswith("_") and isinstance(v, str)
)


def is_client_event(t: str) -> bool:
    return t in CLIENT_EVENTS


def is_server_event(t: str) -> bool:
    return t in SERVER_EVENTS
