"""
bella.devices — the hub-and-client device model, kept out of the tools and the
transport.

  - `TOOL_CAPABILITY`  : which client capability a device tool needs
  - `local_capabilities()` : what THIS machine can do (for the server-mac device)
  - `resolve_target_device()` : parse "... on Windows" / "send this to my iPad"
    and pick the target device (default = the source device)

The runtime asks a `tool_gateway` (server-side, injected) to actually run device
tools; this module only holds the pure decision logic so it is import-safe
everywhere (no websockets, no macOS).
"""

from __future__ import annotations

import re
import socket
import sys
from dataclasses import dataclass

SERVER_DEVICE_ID = "server-mac"

# A device tool -> the capability a client must advertise to run it.
TOOL_CAPABILITY: dict[str, str] = {
    "shell": "shell",
    "read_file": "filesystem",
    "write_file": "filesystem",
    "open_browser": "browser",
    "volume": "volume",
    "spotify": "spotify",
    "reminder": "reminders",
}


def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def local_capabilities() -> list[str]:
    """Capabilities the machine running the server can perform itself (used to
    register `server-mac` as a device). Derived from which tools are actually
    available in the local registry."""
    try:
        from tools.registry import get_registry

        reg = get_registry()
        caps: list[str] = []
        for name, cap in TOOL_CAPABILITY.items():
            tool = reg.get(name)
            if tool is not None and tool.available and cap not in caps:
                caps.append(cap)
        # Server-side capabilities the local box always has.
        for extra in ("notifications",):
            caps.append(extra)
        return caps
    except Exception:
        return ["shell", "filesystem", "browser", "notifications"]


def local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


# ── target-device parsing ─────────────────────────────────────────────────

# "... on windows", "... on my ipad", "... to the mac", "run X on the gaming pc"
_ON_DEVICE_RE = re.compile(
    r"\s+(?:on|to|from|at|onto|over on|over to)\s+"
    r"(?:my |the |our )?"
    r"([a-z0-9][a-z0-9 \-']{0,28}?)"
    r"(?:'s)?\s*$",
    re.IGNORECASE,
)
# "send this to my ipad" style where the device is the object
_SEND_TO_RE = re.compile(
    r"^(?:send|push|forward|open|show|display|throw|cast|move|copy)\b.*?\b"
    r"(?:to|on|onto)\s+(?:my |the |our )?"
    r"([a-z0-9][a-z0-9 \-']{0,28}?)(?:'s)?\s*$",
    re.IGNORECASE,
)
# "mute the mac", "lock windows", "restart the gaming pc" — a machine-control
# verb whose object is a device. Only fires when the object resolves.
_CONTROL_DEVICE_RE = re.compile(
    r"^(?:mute|unmute|lock|unlock|wake|sleep|shut\s?down|shutdown|restart|reboot|"
    r"screenshot|screen\s?shot)\s+(?:the |my |our )?"
    r"([a-z0-9][a-z0-9 \-']{0,28}?)(?:'s)?\s*$",
    re.IGNORECASE,
)

_GENERIC_TAIL = {
    "it", "this", "that", "here", "there", "screen",
    "device", "other one", "please", "now",
}

# A candidate that fails to resolve is only treated as "an unknown device the
# user named" (vs. a topic) when it reads like a device reference.
_DEVICE_NOUN_RE = re.compile(
    r"\b(pc|mac|macbook|laptop|desktop|phone|ipad|iphone|tablet|box|machine|"
    r"computer|server|rig|workstation|surface|chromebook)$",
    re.IGNORECASE,
)
_PLATFORM_WORDS = {"windows", "linux", "macos"}


@dataclass
class TargetResolution:
    device_id: str | None      # None -> couldn't resolve a named device
    device_name: str
    cleaned_text: str          # input with the "... on X" clause removed
    explicit: bool             # user named a device (vs. defaulted to source)


class DeviceLookup:
    """Minimal interface the resolver needs — implemented by the DeviceRegistry.
    Kept as a Protocol-ish duck type so bella.devices imports nothing heavy."""

    def find(self, alias_or_name: str):  # -> Device | None
        raise NotImplementedError

    def get(self, device_id: str):  # -> Device | None
        raise NotImplementedError


def resolve_target_device(
    text: str,
    source_device_id: str | None,
    lookup: "DeviceLookup | None",
) -> TargetResolution:
    """Decide which device a turn's device-local actions should run on.

    Rule: target = source device, UNLESS the user explicitly names another one
    ("open VS Code on Windows"). If they name a device we don't know, keep the
    text intact and let the agent explain.
    """
    stripped = text.strip()
    source_name = source_device_id or SERVER_DEVICE_ID

    if lookup is None:
        return TargetResolution(source_device_id, source_name, text, explicit=False)

    m_on = _ON_DEVICE_RE.search(stripped)
    m = m_on or _SEND_TO_RE.match(stripped) or _CONTROL_DEVICE_RE.match(stripped)
    if not m:
        return TargetResolution(source_device_id, source_name, text, explicit=False)

    candidate = m.group(1).strip().lower()
    if not candidate or candidate in _GENERIC_TAIL:
        return TargetResolution(source_device_id, source_name, text, explicit=False)

    dev = lookup.find(candidate)
    if dev is None:
        had_possessive = bool(re.search(
            r"\b(?:my|our)\s+" + re.escape(candidate) + r"\b", stripped, re.IGNORECASE))
        looks_like_device = bool(_DEVICE_NOUN_RE.search(candidate)) or candidate in _PLATFORM_WORDS
        if had_possessive or looks_like_device:
            # The user named a device we don't have — don't retarget, don't
            # strip; the agent should say it doesn't know that device.
            return TargetResolution(None, candidate, text, explicit=True)
        # Just a topic ("news on windows 11") — not a targeting clause.
        return TargetResolution(source_device_id, source_name, text, explicit=False)

    # Only strip a trailing "... on <device>" clause; a control verb ("mute the
    # mac") keeps its text so the agent still knows what to do.
    if m_on:
        cleaned = (stripped[: m_on.start()] + stripped[m_on.end():]).strip() or text
    else:
        cleaned = stripped
    return TargetResolution(dev.device_id, dev.device_name, cleaned, explicit=True)
