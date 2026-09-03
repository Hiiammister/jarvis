"""
clients.common.protocol — the wire format, shared with the server.

This re-exports the canonical `protocol/` package (stdlib-only, 3 files) so a
client in this repo never re-defines the protocol. A fully standalone client
(e.g. shipped to a Windows box on its own) should vendor the `protocol/`
directory next to `clients/` — nothing else in it depends on `bella`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from protocol import (  # noqa: E402,F401
    PROTOCOL_VERSION,
    C,
    S,
    DeviceInfo,
    Envelope,
    ToolResult,
    decode,
    encode,
    is_server_event,
    new_request_id,
)
