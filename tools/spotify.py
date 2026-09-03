"""tools/spotify.py — control the Spotify desktop app.

macOS-only for now (osascript backend). On other platforms the tool registers
as unavailable and returns a capability error rather than crashing startup.
"""

from __future__ import annotations

from tools.base import PermissionLevel, Tool
from tools.platform import PLATFORM_NAME, macos_backend
from tools.registry import register

_MACOS = macos_backend()

_DESCRIPTION = (
    "Control the Spotify desktop app on macOS. Use this instead of writing "
    "AppleScript by hand — Spotify's AppleScript dictionary has NO search command.\n"
    "Actions:\n"
    "  - open      : launch/focus the Spotify app\n"
    "  - play      : play a specific track. Pass `query` with the song name "
    "(and artist if known), e.g. query='Do I Wanna Know Arctic Monkeys'. "
    "The tool searches Spotify, plays the top match, and returns the real "
    "track it started. With NO query and NO uri it just resumes playback.\n"
    "  - search    : look up tracks for `query` and return their names + URIs "
    "(also opens the Spotify search page). Use this if the user wants choices.\n"
    "  - pause     : pause playback\n"
    "  - next      : skip to next track\n"
    "  - previous  : go to previous track\n"
    "  - current   : report the currently playing track\n"
    "  - quit      : quit/close the Spotify app entirely (not just pause)\n"
    "NEVER invent a `uri` — only pass one that came back from a previous "
    "search/play result. To play by name, use `query`, not `uri`."
)


class SpotifyTool(Tool):
    name = "spotify"
    execution_scope = "device"
    description = _DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "search", "play", "pause", "stop", "next", "previous", "current", "quit"],
                "description": "The Spotify operation. 'stop' is treated as 'pause'.",
            },
            "query": {"type": "string", "description": "Song name / artist to search for (for action=play or action=search)."},
            "uri": {"type": "string", "description": "A Spotify URI that came from an earlier search/play result. Do NOT fabricate this — use `query` to play by name."},
        },
        "required": ["action"],
    }
    permission = PermissionLevel.LOW_RISK

    available = _MACOS is not None
    unavailable_reason = (
        None if _MACOS is not None
        else f"Spotify control is macOS-only for now (running on {PLATFORM_NAME})"
    )

    def execute(self, action: str, query: str = None, uri: str = None) -> dict:
        if _MACOS is None:
            return {"error": self.unavailable_reason}
        return _MACOS.run_spotify(action, query, uri)


register(SpotifyTool())
