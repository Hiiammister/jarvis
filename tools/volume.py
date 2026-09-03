"""tools/volume.py — read/set/adjust/mute the system output volume.

macOS-only for now (osascript backend). On other platforms the tool registers
as unavailable and returns a capability error rather than crashing startup.
"""

from __future__ import annotations

from tools.base import PermissionLevel, Tool
from tools.platform import PLATFORM_NAME, macos_backend
from tools.registry import register

_MACOS = macos_backend()

_DESCRIPTION = (
    "Read or change the macOS system output volume (0-100). Use this for ANY "
    "request about 'volume', 'louder', 'quieter', 'turn it up/down', 'mute', "
    "'unmute' that is NOT explicitly about a specific app's own volume slider.\n"
    "Actions:\n"
    "  - get    : return the current output volume and mute state\n"
    "  - set    : set the absolute volume to `level` (0-100)\n"
    "  - adjust : add `delta` to the current volume (delta may be negative). "
    "For 'increase by 50%' / 'raise it 50%', pass percent=50 instead of delta.\n"
    "  - mute   : mute output\n"
    "  - unmute : unmute output\n"
    "Every action returns the resulting volume, so you always know the real state."
)


class VolumeTool(Tool):
    name = "volume"
    execution_scope = "device"
    description = _DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "adjust", "mute", "unmute"],
                "description": "The volume operation.",
            },
            "level": {"type": "integer", "description": "Absolute target volume 0-100 (for action=set)."},
            "delta": {"type": "integer", "description": "Points to add to the current volume, may be negative (for action=adjust)."},
            "percent": {"type": "number", "description": "Relative change as a percentage of the current volume, e.g. 50 raises 40->60, -25 lowers it a quarter (for action=adjust)."},
        },
        "required": ["action"],
    }
    permission = PermissionLevel.LOW_RISK

    available = _MACOS is not None
    unavailable_reason = (
        None if _MACOS is not None
        else f"system volume control is macOS-only (running on {PLATFORM_NAME})"
    )

    def execute(self, action: str, level: int = None, delta: int = None, percent: float = None) -> dict:
        if _MACOS is None:
            return {"error": self.unavailable_reason}
        return _MACOS.run_volume(action, level, delta, percent)


register(VolumeTool())
