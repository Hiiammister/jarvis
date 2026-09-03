"""
protocol.models — the Envelope every message rides in, plus payload helpers.

    {
      "type": "message",
      "v": 1,
      "request_id": "r-ab12cd34",
      "device_id": "windows-desktop",
      "ts": 1725500000.12,
      "payload": { ... }
    }

Encoded as a single JSON object per WebSocket text frame. `encode`/`decode` are
the only functions transports should use.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# Guardrails against a hostile / buggy peer.
MAX_FRAME_BYTES = 256 * 1024
MAX_TEXT_CHARS = 16_000


def now_ts() -> float:
    return round(time.time(), 3)


def new_request_id() -> str:
    return "r-" + secrets.token_hex(5)


@dataclass
class Envelope:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    device_id: str = ""
    v: int = PROTOCOL_VERSION
    ts: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "v": self.v,
            "request_id": self.request_id,
            "device_id": self.device_id,
            "ts": self.ts,
            "payload": self.payload,
        }

    @classmethod
    def make(cls, type: str, payload: dict | None = None, *,
             request_id: str = "", device_id: str = "") -> "Envelope":
        return cls(type=type, payload=payload or {},
                   request_id=request_id, device_id=device_id)


class ProtocolError(ValueError):
    """Malformed / oversized / wrong-version frame."""


def encode(env: Envelope) -> str:
    return json.dumps(env.to_dict(), separators=(",", ":"), ensure_ascii=False)


def decode(raw: str | bytes) -> Envelope:
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        raw = raw.decode("utf-8", "replace")
    elif len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")

    try:
        obj = json.loads(raw)
    except Exception as e:
        raise ProtocolError(f"not JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not an object")

    etype = obj.get("type")
    if not isinstance(etype, str) or not etype:
        raise ProtocolError("missing type")
    v = obj.get("v", PROTOCOL_VERSION)
    if not isinstance(v, int) or v != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {v!r}")
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")

    return Envelope(
        type=etype,
        payload=payload,
        request_id=str(obj.get("request_id") or ""),
        device_id=str(obj.get("device_id") or ""),
        v=v,
        ts=float(obj.get("ts") or now_ts()),
    )


# ── payload dataclasses ────────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Sent by a client in its `hello`. `device_id` should be stable for a
    given physical device (hostname-derived is fine)."""

    device_id: str
    device_name: str
    platform: str                       # "macos" | "windows" | "linux" | "web"
    client_type: str = "desktop"        # "desktop" | "web" | "server"
    hostname: str = ""
    capabilities: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> "DeviceInfo":
        did = str(p.get("device_id") or "").strip()
        if not did:
            raise ProtocolError("hello: device_id required")
        return cls(
            device_id=did[:64],
            device_name=str(p.get("device_name") or did)[:64],
            platform=str(p.get("platform") or "unknown")[:32],
            client_type=str(p.get("client_type") or "desktop")[:16],
            hostname=str(p.get("hostname") or "")[:128],
            capabilities=[str(c)[:32] for c in (p.get("capabilities") or [])][:32],
            aliases=[str(a)[:32].lower() for a in (p.get("aliases") or [])][:16],
            protocol_version=int(p.get("protocol_version") or PROTOCOL_VERSION),
        )


@dataclass
class ToolResult:
    """A client's answer to `device_tool_request`. `result` is the same dict
    shape the server-side tool would have returned."""

    request_id: str
    tool: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
