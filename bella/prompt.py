"""
bella.prompt — system-prompt construction.

Combines SOUL.md (personality), the frozen persistent-memory block, the current
date/time, and — for tool-using routes — the strict tool-use rules.

`build_system_prompt` takes two latency-oriented knobs:
  minimal=True   -> personality + memory + time only, NO tool rules (FAST_CHAT)
  tool_names=[...] -> emit only the rules for those tools (scoped FULL_AGENT)

Behaviour for the default full call (no args) is unchanged — every rule, in the
original order. The voice_mode branch on the Spotify rule is preserved verbatim
(a live mic transcript can pick up song lyrics that look like commands).
"""

from __future__ import annotations

from datetime import datetime

from memory.core import build_memory_block

from bella.config import REPO_ROOT

SOUL_FILE = REPO_ROOT / "SOUL.md"


def load_soul() -> str:
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8")
    return "You are Bella, a helpful AI agent."


def _spotify_rule(voice_mode: bool) -> str:
    if voice_mode:
        return (
            '- If the user says "play", "pause", "stop", "skip", "next", "previous", "close/quit Spotify", or '
            'names a song/artist to play — call the `spotify` tool, but ONLY if it is an unmistakable, deliberate '
            'command directed at you. This transcript came from a live microphone that can pick up Spotify\'s own '
            'music playing through the speakers — song lyrics often contain exactly these words. If the phrase '
            'could plausibly be a lyric, background chatter, or isn\'t clearly an instruction to you, do NOT call '
            'spotify and do not acknowledge it as a command at all — treat it as noise. To play a named song, use '
            'action=play with query="<song and artist>". For "stop", use action=pause; for "close"/"quit '
            "Spotify\", use action=quit — that's the only action that actually closes the app, pause/stop do not. "
            'NEVER pass a `uri` you made up — only a `uri` returned by a previous spotify result. After the call, '
            "report the exact track/outcome from the tool result; if `success` is false, say so and why."
        )
    return (
        '- If the user says "play", "pause", "stop", "skip", "next", "previous", "close/quit Spotify", or '
        'names a song/artist to play — call the `spotify` tool. To play a named song, use action=play with '
        'query="<song and artist>". For "stop", use action=pause; for "close"/"quit Spotify", use action=quit '
        "— that's the only action that actually closes the app, pause/stop do not. NEVER pass a `uri` you "
        "made up — only a `uri` returned by a previous spotify result. After the call, report the exact "
        "track from the tool result's `track` field; if `success` is false, tell the user it did not play "
        "and why."
    )


# Per-tool rule fragments, keyed by the tool they concern. Order preserved from
# the original monolithic prompt.
def _tool_rule(name: str, voice_mode: bool) -> str:
    return {
        "web_search": '- If the user says "search", "look up", "google", "find", or asks about any fact/event/person — call web_search immediately. Never answer from memory alone.',
        "shell": '- If the user asks you to run something, create a file, or do anything on the computer — call shell immediately.',
        "open_browser": '- If the user says "open", "go to", "visit", or mentions a website/URL — call open_browser immediately.',
        "volume": '- If the user mentions "volume", "louder", "quieter", "turn it up/down", "mute", "unmute" — call the `volume` tool. For "increase/raise/lower by N%", use action=adjust with percent=N.',
        "spotify": _spotify_rule(voice_mode),
        "reminder": "- If the user says \"remind me\", \"set a reminder\", \"add a reminder\", or similar — call the `reminder` tool (action=add), NEVER the `todo` tool (that's a separate local list the real Reminders app never sees). Compute any time the user gives (\"at 8pm\", \"tomorrow morning\", \"in an hour\") into an absolute due_date='YYYY-MM-DD HH:MM' yourself using the CURRENT DATE/TIME given in this prompt — never guess or leave it symbolic. If no time is given, omit due_date. Report back only what the tool result actually confirms (added / due_date / list).",
        "github": '- If the user says "upload to GitHub", "push to GitHub", "back this up", "sync to GitHub", or similar — call the `github` tool (action=upload). NEVER use `shell` to hand-run git/gh commands for this — the tool handles committing, first-time repo creation, and pushing in one step, and reports exactly what happened. If it fails (e.g. not authenticated), tell the user the real error and what to do (usually: run `gh auth login`) — do not claim it uploaded.',
    }.get(name, "")


# Always included whenever any tool is available — general execution discipline.
_GENERIC_TOOL_RULES = """- If a shell command fails, automatically try the next logical command. Do not stop and ask.
- NEVER say "Would you like me to X?" or "Should I X?" — just do it.
- NEVER list steps you plan to take. Execute them.
- Do not describe what you would do. Do it.
- NEVER claim an action succeeded unless a tool call actually returned a success result. If you did not call a tool, you did not do it — call the tool now. If a tool returns an error, report the error; do not pretend it worked."""

# The full ordered rule set (default when tool_names is None).
_ALL_TOOL_ORDER = ["web_search", "shell", "open_browser", "volume", "spotify",
                   "reminder", "github"]


def _tool_rules_block(voice_mode: bool, tool_names: list[str] | None) -> str:
    if tool_names is None:
        names = _ALL_TOOL_ORDER
    else:
        wanted = set(tool_names)
        names = [n for n in _ALL_TOOL_ORDER if n in wanted]
        # `todo` shares the reminder rule; `recall`/`memory`/`read_file`/
        # `write_file` need no special rule beyond the generic discipline.
        if "todo" in wanted and "reminder" not in wanted:
            names.append("reminder")
    fragments = [_tool_rule(n, voice_mode) for n in names]
    fragments = [f for f in fragments if f]
    body = "\n".join(fragments + [_GENERIC_TOOL_RULES])
    return f"\n## TOOL USE RULES (follow strictly)\n{body}\n"


def build_system_prompt(
    voice_mode: bool = False,
    *,
    tool_names: list[str] | None = None,
    minimal: bool = False,
) -> str:
    """Build the system prompt.

    Order: personality, tool rules, memory, then the current date/time LAST.
    The volatile time line (minute granularity) sits at the end so everything
    before it — soul + tool rules + memory, the bulk of the prompt — stays a
    stable prefix that Ollama's KV cache can reuse across turns.

    - default: all tool rules.
    - minimal=True: no tool rules (FAST_CHAT).
    - tool_names=[...]: only those tools' rules (scoped FULL_AGENT).
    """
    soul = load_soul()
    memory_block = build_memory_block()
    now = datetime.now()
    time_block = f"## CURRENT DATE/TIME\n{now.strftime('%A, %Y-%m-%d %H:%M')}"

    if minimal:
        return f"{soul}\n\n{memory_block}\n\n{time_block}"
    rules = _tool_rules_block(voice_mode, tool_names)
    return f"{soul}\n{rules}\n{memory_block}\n\n{time_block}"
