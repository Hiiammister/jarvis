"""
bella.interfaces.theme — the single place the CLI's look is defined.

Colours, symbols, the BELLA logo, and the tool-name -> human-activity map.
Nothing here does any rendering; `renderer.py` consumes it.

Palette: dark-terminal, teal/cyan accent, Bella's name always the brightest
teal. Response text is left unstyled so it stays readable on any background.
Truecolor hexes degrade gracefully to 256/16-colour terminals via Rich.
"""

from __future__ import annotations

from rich.theme import Theme

# ── palette ─────────────────────────────────────────────────────────────────

BELLA_COLOR = "#5eead4"      # bright teal — Bella's identity
USER_COLOR = "#e5e7eb"       # near-white — the user
ACCENT_COLOR = "#2dd4bf"     # teal — markers, section headers, prompt caret
SUCCESS_COLOR = "#4ade80"
WARNING_COLOR = "#fbbf24"
ERROR_COLOR = "#f87171"
MUTED_COLOR = "#6b7280"      # grey — secondary text, timings, hints
RESPONSE_COLOR = "#d4d4d8"   # light neutral — assistant prose

# Rich style names — use as markup: "[bella]Bella[/]", "[muted]…[/]"
RICH_THEME = Theme(
    {
        "bella": f"bold {BELLA_COLOR}",
        "user": f"bold {USER_COLOR}",
        "accent": ACCENT_COLOR,
        "accent.bold": f"bold {ACCENT_COLOR}",
        "success": SUCCESS_COLOR,
        "warning": WARNING_COLOR,
        "error": f"bold {ERROR_COLOR}",
        "muted": MUTED_COLOR,
        "response": RESPONSE_COLOR,
        "heading": f"bold {ACCENT_COLOR}",
    }
)

# Vertical gradient for the 6 logo rows (teal -> deeper teal).
LOGO_GRADIENT = ["#5eead4", "#4fd9c9", "#41c9bd", "#34b8b0", "#2aa79f", "#22968d"]


# ── symbols (used sparingly, always the same meaning) ───────────────────────

CARET = "›"          # prompt caret:  You ›   Bella ›
THINKING = "◌"       # Bella is thinking
TOOL = "◈"           # a tool is running / a tool line
OK = "✓"             # success
FAIL = "✗"           # failure / error
WARN = "⚠"           # warning / degraded
IDLE = "○"           # inactive / optional / not running
MIC = "🎙"           # listen-mode idle prompt (one of only two emoji used)
SPEAK = "🔊"         # Bella is speaking a reply aloud (the other)


# ── tool name -> human activity label ──────────────────────────────────────
#
# What the spinner says while a tool runs ("Bella ◈ searching web…"). The raw
# tool name is only shown in verbose mode.

TOOL_ACTIVITY = {
    "web_search": "searching web",
    "shell": "running command",
    "read_file": "reading file",
    "write_file": "writing file",
    "open_browser": "opening browser",
    "volume": "adjusting volume",
    "spotify": "controlling Spotify",
    "reminder": "updating reminders",
    "todo": "updating tasks",
    "memory": "updating memory",
    "github": "checking GitHub",
    "recall": "searching memory",
}

# Short, human tool titles for result lines ("◈ Spotify", "◈ shell").
TOOL_TITLE = {
    "web_search": "Web search",
    "shell": "shell",
    "read_file": "Read file",
    "write_file": "Write file",
    "open_browser": "Browser",
    "volume": "Volume",
    "spotify": "Spotify",
    "reminder": "Reminders",
    "todo": "Tasks",
    "memory": "Memory",
    "github": "GitHub",
    "recall": "Memory search",
}


def activity_label(tool_name: str) -> str:
    return TOOL_ACTIVITY.get(tool_name, f"running {tool_name}")


def tool_title(tool_name: str) -> str:
    return TOOL_TITLE.get(tool_name, tool_name)


# ── logo ────────────────────────────────────────────────────────────────────

LOGO = r"""██████╗ ███████╗██╗     ██╗      █████╗
██╔══██╗██╔════╝██║     ██║     ██╔══██╗
██████╔╝█████╗  ██║     ██║     ███████║
██╔══██╗██╔══╝  ██║     ██║     ██╔══██║
██████╔╝███████╗███████╗███████╗██║  ██║
╚═════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝"""

TAGLINE = "Personal AI Workstation"


# ── raw ANSI (only for the threaded spinner, which needs cursor control) ────
#
# Everything else goes through Rich. The spinner writes \r / \033[K directly so
# it can repaint one line in place without Rich's Live machinery.

def _fg(hex_color: str) -> str:
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"\033[38;2;{r};{g};{b}m"


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_BELLA = ANSI_BOLD + _fg(BELLA_COLOR)
ANSI_ACCENT = _fg(ACCENT_COLOR)
ANSI_MUTED = _fg(MUTED_COLOR)
ANSI_RESPONSE = _fg(RESPONSE_COLOR)
ANSI_CLEAR_LINE = "\r\033[K"
