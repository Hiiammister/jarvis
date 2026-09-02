#!/usr/bin/env python3
"""
jarvis.py — Main entry point.

Usage:
    python jarvis.py
    python jarvis.py "do something specific"   # single-shot mode
"""

import os
import re
import sys
import json
from datetime import datetime
from pathlib import Path

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.markup import escape as _rich_escape
from rich import print as rprint
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from memory.core import Session, build_memory_block
from llm.ollama_client import run_agent_turn, MODEL, BASE_OPTIONS
from tools.executor import execute_tool
from tools import voice as _voice_mod  # trial feature — see tools/voice.py to remove
from tools.voice import speak
from tools.ears import listen as listen_mic  # trial feature — see tools/ears.py to remove

# Background memory-extraction threads, joined on shutdown so they can't be
# killed mid-write (which would corrupt MEMORY.md).
_bg_threads: list = []


def _spawn_bg(target, *args):
    import threading
    _bg_threads[:] = [t for t in _bg_threads if t.is_alive()]
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    _bg_threads.append(t)
    return t


def _join_bg(timeout: float = 20.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    for t in _bg_threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)

console = Console()
JARVIS_HOME = Path.home() / ".jarvis"


class _Spinner:
    """
    Persistent 'Bella … thinking' indicator for the length of one turn.

    Unlike a one-shot spinner, this stays available across every agent-loop
    iteration: it is paused only while assistant text is actively streaming and
    resumed (with a contextual label) around each tool call, so subprocess-heavy
    tools never leave the REPL looking frozen. All stdout writes go through one
    lock so the animation can't interleave with tool-event output.
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "thinking..."):
        import threading
        self.label = label
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import itertools
        import time
        frames = itertools.cycle(self._FRAMES)
        while not self._stop.is_set():
            if not self._paused.is_set():
                with self._lock:
                    # Re-check under the lock: pause() may have fired (and text
                    # may already be streaming) while we waited to acquire it.
                    # Painting now would clobber that text with our \r…\033[K.
                    if not self._paused.is_set():
                        sys.stdout.write(
                            f"\r\033[1;36mBella\033[0m {next(frames)} "
                            f"\033[2m{self.label}\033[0m\033[K"
                        )
                        sys.stdout.flush()
            time.sleep(0.08)
        # Only wipe our line if the spinner is currently showing. If we're
        # paused, the cursor is sitting on streamed reply text — erasing that
        # line would make the whole answer flash and disappear.
        if not self._paused.is_set():
            with self._lock:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

    def start(self):
        self._thread.start()

    def pause(self):
        """Hide the spinner and clear its line. Idempotent."""
        if self._paused.is_set():
            return
        self._paused.set()
        with self._lock:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def resume(self, label: str = None):
        if label is not None:
            self.label = label
        self._paused.clear()

    def write(self, text: str):
        """Emit text on the same lock the animation uses, so nothing garbles."""
        with self._lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def stop(self):
        self._stop.set()
        self._thread.join()
SOUL_FILE = Path(__file__).parent / "SOUL.md"

PROMPT_STYLE = Style.from_dict({
    "prompt": "bold cyan",
})

BANNER = """[bold cyan]
██████╗ ███████╗██╗     ██╗      █████╗
██╔══██╗██╔════╝██║     ██║     ██╔══██╗
██████╔╝█████╗  ██║     ██║     ███████║
██╔══██╗██╔══╝  ██║     ██║     ██╔══██║
██████╔╝███████╗███████╗███████╗██║  ██║
╚═════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝
[/bold cyan][dim]  Claude-powered AI agent with persistent memory[/dim]
"""

SLASH_COMMANDS = {
    "/help":    "Show this help",
    "/memory":  "Show current memory contents",
    "/history": "Show this session's messages",
    "/recall":  "Search past sessions: /recall <query>",
    "/clear":   "Clear the conversation (memory is preserved)",
    "/listen":  "Voice mode: say \"Bella\" or press Enter, spoken replies (default) (trial)",
    "/silent":  "Text mode: type, read replies, no voice at all (trial)",
    "/tts":     "Type your input, but Bella speaks the replies (trial)",
    "/exit":    "Exit Bella",
    "/quit":    "Exit Bella",
}

# Spoken equivalents of /silent, /tts, /listen, recognized only while in
# listen mode (since that's the one mode where typing a slash command isn't
# an option). Exact-phrase match after light normalization — deliberately
# not fuzzy, so a real command never gets mistaken for a mode switch.
_MODE_VOICE_PHRASES = {
    "silent": {"silent mode", "switch to silent mode", "switch to silent",
               "go silent", "text mode", "switch to text mode",
               "stop listening", "text only"},
    "tts": {"tts mode", "switch to tts mode", "switch to tts",
            "text to speech mode", "voice reply mode", "talk mode"},
    "listen": {"listen mode", "switch to listen mode", "voice mode",
               "switch to voice mode", "start listening"},
}


def _match_mode_phrase(transcript: str) -> str | None:
    t = transcript.strip().lower().strip(".!?")
    for mode, phrases in _MODE_VOICE_PHRASES.items():
        if t in phrases:
            return mode
    return None


# Requires the wake word as a PREFIX (re.match, not search) — "bella" showing
# up mid-sentence in unrelated speech/lyrics still doesn't count as being
# addressed. The greeting word is optional ("Bella, ..." works, not just
# "Hey Bella, ...").
_WAKE_WORD_RE = re.compile(r"^(hey|hi|ok|okay)?[\s,]*bella\b[\s,.:!?]*", re.IGNORECASE)


def _strip_wake_word(transcript: str) -> tuple[bool, str]:
    """Returns (was_addressed_to_bella, remaining_text_after_the_wake_word).
    remaining_text is "" when the utterance was just the wake word alone."""
    m = _WAKE_WORD_RE.match(transcript.strip())
    if not m:
        return False, transcript
    return True, transcript[m.end():].strip()


def _duck_spotify() -> bool:
    """Pause Spotify if it's currently playing, so its own audio can't bleed
    into the mic during a listen cycle and get mistaken for a command —
    same problem as Bella's own voice, different source. Returns whether it
    was actually playing (i.e. whether the caller should resume it after)."""
    try:
        result = json.loads(execute_tool("spotify", {"action": "current"}))
    except Exception:
        return False
    if result.get("player_state") != "playing":
        return False
    execute_tool("spotify", {"action": "pause"})
    return True


def _unduck_spotify() -> None:
    execute_tool("spotify", {"action": "play"})  # no query/uri = resume


# Shorter than the idle wake-word cycle's 20s default — this capture only
# starts once the user has already signaled intent (wake word or Enter), so
# if nothing coherent follows, give up sooner rather than sitting there
# looking hung for the full 20s.
_COMMAND_CAPTURE_SECONDS = 10.0


def _capture_command() -> str:
    """Duck Spotify, actively record one command from the mic, unduck.
    Used once Bella's already been addressed (wake word confirmed, or a
    blank Enter) — no wake word needed for this specific recording.

    Shows a live spinner (previously this call had zero feedback and no
    way to bail out, which made it look identical to a hang) and stays
    cancellable by pressing Enter again, same mechanism as the idle
    wake-word race."""
    import select
    import threading

    was_playing = _duck_spotify()
    stop_event = threading.Event()
    result = {"text": ""}

    def _worker():
        result["text"] = listen_mic(stop_event=stop_event, max_seconds=_COMMAND_CAPTURE_SECONDS)

    worker = threading.Thread(target=_worker, daemon=True)
    spinner = _Spinner("listening...")
    spinner.start()
    worker.start()
    try:
        while worker.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                sys.stdin.readline()  # consume it — pressing Enter here means "never mind"
                stop_event.set()
                break
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        worker.join(timeout=3.0)
        spinner.stop()
        if was_playing:
            _unduck_spotify()
    return result["text"]


def _wait_for_wake_or_enter() -> tuple[str | None, str | None]:
    """Idle-listen for "Bella ..." while simultaneously watching the
    keyboard for a plain Enter press — whichever comes first wins. Returns
    (heard_transcript, typed_line): exactly one is non-None on a normal
    return (both None only if interrupted).

    Why this shape: a blocking mic read can be cancelled early via
    tools.ears's stop_event (checked every ~30ms), but a blocking
    keyboard read (input()/PromptSession.prompt()) can't be cancelled
    from another thread without deeper terminal surgery. So the keyboard
    side only ever *polls* stdin non-blockingly (`select`, checked every
    100ms) instead of committing to a real blocking read — there's never
    a stuck call on that side to cancel in the first place, and the race
    stays simple: on Enter, stop the mic thread; on the mic finishing
    first, there's nothing pending on the keyboard side to clean up."""
    import select
    import threading

    stop_event = threading.Event()
    result: dict = {"heard": None}

    def _mic_worker():
        result["heard"] = listen_mic(stop_event=stop_event)

    mic_thread = threading.Thread(target=_mic_worker, daemon=True)
    mic_thread.start()

    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                typed = sys.stdin.readline().rstrip("\n")
                stop_event.set()
                mic_thread.join(timeout=2.0)
                return None, typed
            if not mic_thread.is_alive():
                return result["heard"], None
    except KeyboardInterrupt:
        stop_event.set()
        mic_thread.join(timeout=2.0)
        raise


def load_soul() -> str:
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8")
    return "You are Bella, a helpful AI agent."


def build_system_prompt(voice_mode: bool = False) -> str:
    soul = load_soul()
    memory_block = build_memory_block()
    now = datetime.now()
    time_block = f"\n## CURRENT DATE/TIME\n{now.strftime('%A, %Y-%m-%d %H:%M')}\n"

    # In listen mode, the mic can pick up Spotify's own music playing through
    # the speakers — song lyrics routinely contain words like "stop", "play",
    # "go". The text-mode rule below is loose on purpose (natural typed
    # phrasing varies), but a typed message is always a deliberate command;
    # a live transcript might just be a lyric bleeding through. So voice mode
    # gets the same effective precision as text's fast-path matcher: only
    # act on an unmistakable command, not merely a plausible keyword.
    if voice_mode:
        spotify_rule = (
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
    else:
        spotify_rule = (
            '- If the user says "play", "pause", "stop", "skip", "next", "previous", "close/quit Spotify", or '
            'names a song/artist to play — call the `spotify` tool. To play a named song, use action=play with '
            'query="<song and artist>". For "stop", use action=pause; for "close"/"quit Spotify", use action=quit '
            "— that's the only action that actually closes the app, pause/stop do not. NEVER pass a `uri` you "
            "made up — only a `uri` returned by a previous spotify result. After the call, report the exact "
            "track from the tool result's `track` field; if `success` is false, tell the user it did not play "
            "and why."
        )

    tool_rules = f"""
## TOOL USE RULES (follow strictly)
- If the user says "search", "look up", "google", "find", or asks about any fact/event/person — call web_search immediately. Never answer from memory alone.
- If the user asks you to run something, create a file, or do anything on the computer — call shell immediately.
- If the user says "open", "go to", "visit", or mentions a website/URL — call open_browser immediately.
- If the user mentions "volume", "louder", "quieter", "turn it up/down", "mute", "unmute" — call the `volume` tool. For "increase/raise/lower by N%", use action=adjust with percent=N.
{spotify_rule}
- If the user says "remind me", "set a reminder", "add a reminder", or similar — call the `reminder` tool (action=add), NEVER the `todo` tool (that's a separate local list the real Reminders app never sees). Compute any time the user gives ("at 8pm", "tomorrow morning", "in an hour") into an absolute due_date='YYYY-MM-DD HH:MM' yourself using the CURRENT DATE/TIME above — never guess or leave it symbolic. If no time is given, omit due_date. Report back only what the tool result actually confirms (added / due_date / list).
- If a shell command fails, automatically try the next logical command. Do not stop and ask.
- NEVER say "Would you like me to X?" or "Should I X?" — just do it.
- NEVER list steps you plan to take. Execute them.
- Do not describe what you would do. Do it.
- NEVER claim an action succeeded unless a tool call actually returned a success result. If you did not call a tool, you did not do it — call the tool now. If a tool returns an error, report the error; do not pretend it worked.
"""
    return f"{soul}\n{time_block}\n{tool_rules}\n{memory_block}"


def print_banner():
    console.print(BANNER)
    console.print(
        f"[dim]Model: [bold]{MODEL}[/bold]  ·  Type your message, or [bold]/help[/bold] for commands. "
        "[bold]Ctrl+C[/bold] or [bold]/exit[/bold] to quit.[/dim]\n"
    )


def show_help():
    console.print(Rule("[bold]Commands[/bold]", style="cyan"))
    for cmd, desc in SLASH_COMMANDS.items():
        console.print(f"  [bold cyan]{cmd:<12}[/bold cyan] {desc}")
    console.print()


def show_memory():
    from memory.core import memory_read
    console.print(Rule("[bold]Persistent Memory[/bold]", style="cyan"))
    for target, label in [("memory", "MEMORY.md"), ("user", "USER.md")]:
        result = memory_read(target)
        pct = result["pct"]
        chars = result["chars"]
        limit = result["limit"]
        console.print(f"\n[bold]{label}[/bold] [{pct}% — {chars}/{limit} chars]")
        if result["entries"]:
            for i, entry in enumerate(result["entries"], 1):
                console.print(f"  [dim]{i}.[/dim] {entry}")
        else:
            console.print("  [dim](empty)[/dim]")
    console.print()


def show_history(session_messages: list[dict]):
    console.print(Rule("[bold]Session History[/bold]", style="cyan"))
    if not session_messages:
        console.print("[dim]No messages yet.[/dim]\n")
        return
    for msg in session_messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, list):
            # Tool result messages — skip display
            continue
        color = "cyan" if role == "assistant" else "white"
        label = "Bella" if role == "assistant" else "You"
        console.print(f"\n[bold {color}]{label}:[/bold {color}]")
        console.print(content[:500] + ("..." if len(content) > 500 else ""))
    console.print()


def handle_recall(query: str):
    from memory.core import search_history
    results = search_history(query, limit=5)
    console.print(Rule(f"[bold]Recall: {query}[/bold]", style="cyan"))
    if not results:
        console.print("[dim]No matching messages found.[/dim]\n")
        return
    for r in results:
        role_label = "Bella" if r["role"] == "assistant" else "You"
        console.print(f"\n[dim]{r['ts']} | {role_label}[/dim]")
        console.print(r["content"][:300] + ("..." if len(r["content"]) > 300 else ""))
    console.print()


# ── Tool event handlers (printed during agent loop) ───────────────────────────

def on_tool_start(name: str, inputs: dict):
    # Build a readable summary of what the tool is doing
    summary = {
        "shell":        lambda: f"$ {inputs.get('command', '')}",
        "web_search":   lambda: f"🔍 {inputs.get('query', '')}",
        "read_file":    lambda: f"📄 {inputs.get('path', '')}",
        "write_file":   lambda: f"✏️  {inputs.get('path', '')}",
        "open_browser": lambda: f"🌐 {inputs.get('url', '')}",
        "volume":       lambda: (
            f"🔊 {inputs.get('action', '')} " + (
                f"{inputs['level']}" if inputs.get("level") is not None
                else f"{inputs['delta']:+d}" if inputs.get("delta") is not None
                else f"{inputs['percent']:+g}%" if inputs.get("percent") is not None
                else ""
            )
        ).strip(),
        "spotify":      lambda: f"🎧 {inputs.get('action', '')} {inputs.get('query') or inputs.get('uri') or ''}".strip(),
        "todo":         lambda: f"📋 {inputs.get('action', '')} {inputs.get('text') or inputs.get('position', '') or ''}".strip(),
        "reminder":     lambda: f"⏰ {inputs.get('action', '')} {inputs.get('text') or ''}".strip() + (f" @ {inputs['due_date']}" if inputs.get("due_date") else ""),
        "memory":       lambda: f"🧠 {inputs.get('action', '')} → {inputs.get('target', '')}",
        "recall":       lambda: f"🔎 recall: {inputs.get('query', '')}",
    }.get(name, lambda: json.dumps(inputs))()

    console.print(f"\n[dim bold]▶ {name}[/dim bold] [dim]{summary}[/dim]")


def on_tool_end(name: str, result_str: str):
    try:
        result = json.loads(result_str)
    except Exception:
        result = {"raw": result_str}

    # Show compact summaries for common tools
    if name == "shell":
        stdout = result.get("stdout", "").strip()
        stderr = result.get("stderr", "").strip()
        exit_code = result.get("exit_code", 0)
        if stdout:
            lines = stdout.split("\n")
            preview = "\n".join(lines[:10])
            if len(lines) > 10:
                preview += f"\n[dim]... ({len(lines)-10} more lines)[/dim]"
            console.print(f"[dim]{preview}[/dim]")
        if stderr:
            console.print(f"[dim red]{stderr[:300]}[/dim red]")
        if exit_code != 0:
            console.print(f"[dim red]exit {exit_code}[/dim red]")

    elif name == "web_search":
        results = result.get("results", [])
        for r in results[:3]:
            console.print(f"  [dim cyan]{r.get('title','')}[/dim cyan]")
            console.print(f"  [dim]{r.get('snippet','')[:120]}[/dim]")

    elif name == "todo":
        if "todos" in result:
            todos = result["todos"]
            for i, t in enumerate(todos, 1):
                mark = "✓" if t.get("done") else "○"
                console.print(f"  [dim]{i}. [{mark}] {t['text']}[/dim]")
        elif result.get("success"):
            console.print(f"  [dim green]✓ Done[/dim green]")

    elif name == "reminder":
        if "reminders" in result:
            for i, r in enumerate(result["reminders"], 1):
                due = f" — {r['due']}" if r.get("due") else ""
                console.print(f"  [dim]{i}. {r['name']}{due}[/dim]")
        elif result.get("success"):
            console.print(f"  [dim green]✓ Done[/dim green]")
        elif "error" in result:
            console.print(f"  [dim red]{result['error']}[/dim red]")
            if result.get("candidates"):
                console.print(f"  [dim red]  candidates: {', '.join(result['candidates'])}[/dim red]")

    elif name == "memory":
        if result.get("success"):
            chars = result.get("chars", 0)
            limit = result.get("limit", 0)
            console.print(f"  [dim green]✓ {chars}/{limit} chars[/dim green]")
        elif "error" in result:
            console.print(f"  [dim red]{result['error']}[/dim red]")

    elif name in ("read_file", "write_file"):
        if result.get("success") or result.get("content"):
            console.print(f"  [dim green]✓[/dim green]")
        elif "error" in result:
            console.print(f"  [dim red]{result['error']}[/dim red]")


# ── Main REPL ──────────────────────────────────────────────────────────────────

def run_repl(single_shot: str = None):
    import time
    print_banner()

    JARVIS_HOME.mkdir(parents=True, exist_ok=True)
    history_file = JARVIS_HOME / "prompt_history"

    session_db = Session()
    messages: list[dict] = []

    console.print("[dim]Memory loaded. Session started.[/dim]\n")

    if single_shot:
        # Non-interactive single-shot mode — a CLI arg is deliberate typed
        # input like text mode, not a live mic transcript, so it gets the
        # normal (non-tightened) spotify schema.
        system = build_system_prompt(voice_mode=False)
        _handle_input(single_shot, messages, system, session_db)  # (messages, speak_thread) — both unused here
        _join_bg()
        session_db.close()
        return

    system = build_system_prompt(voice_mode=True)  # interactive default mode is "listen" — see below

    prompt_session = PromptSession(
        history=FileHistory(str(history_file)),
        style=PROMPT_STYLE,
    )

    # "listen"  — wake-word voice input ("Hey Bella, ..."), spoken replies
    #             (default: a full voice conversation, idle otherwise)
    # "silent"  — typed input, text replies, no voice at all
    # "tts"     — typed input, but replies are spoken
    # Switch via /listen, /silent, /tts (typed), or — only while already in
    # listen mode, since typing isn't the input method there — by saying
    # "Hey Bella, silent mode" / "Hey Bella, tts mode" / "Hey Bella, listen mode".
    mode = "listen"
    console.print("[dim]Voice mode by default — say \"Bella\" or press Enter to talk "
                  "(e.g. \"Bella, silent mode\" or type directly to switch).[/dim]")

    try:
        while True:
            if mode == "listen":
                _voice_mod.VOICE_ENABLED = True
                # Idle listening never ducks Spotify or acts on anything —
                # only a confirmed "Bella" (or a plain Enter press) does.
                # Otherwise every loud ambient sound (music included) would
                # pause playback and risk being treated as a command;
                # requiring a deliberate trigger fixes both at once.
                console.print("[dim]🎤 Say \"Bella\" or press Enter…[/dim]")
                try:
                    heard, typed = _wait_for_wake_or_enter()
                except KeyboardInterrupt:
                    console.print("\n[dim]Use /exit to quit, or say \"silent mode\" to type instead.[/dim]")
                    continue

                if typed is not None:
                    if typed.strip():
                        # Typed a real command directly — just use it, no
                        # need to also capture from the mic.
                        command = typed.strip()
                    else:
                        # Blank Enter — actively engage: duck Spotify and
                        # capture the command, no wake word needed since
                        # the keypress already signals intent.
                        console.print("[dim]🎤 Listening…[/dim]")
                        command = _capture_command()
                        if not command:
                            console.print("[dim red](didn't catch a command)[/dim red]")
                            continue
                else:
                    if not heard:
                        continue  # ambient noise — stay idle, no need to comment on it

                    addressed, command = _strip_wake_word(heard)
                    if not addressed:
                        console.print(f'[dim](not addressed to Bella: "{_rich_escape(heard[:60])}")[/dim]')
                        continue

                    if not command:
                        # Just the wake word, nothing yet — now actively
                        # engage the same way the Enter path does.
                        console.print("[dim]🎤 Yes?[/dim]")
                        command = _capture_command()
                        if not command:
                            console.print("[dim red](didn't catch a command)[/dim red]")
                            continue

                switched = _match_mode_phrase(command)
                if switched:
                    mode = switched
                    system = build_system_prompt(voice_mode=(mode == "listen"))
                    console.print(f"[dim]Switched to {mode} mode.[/dim]")
                    continue

                console.print(f"[dim]You › {_rich_escape(command)}[/dim]")
                messages, speak_thread = _handle_input(command, messages, system, session_db)
                if speak_thread is not None:
                    # Wait for Bella to actually finish talking before the
                    # mic goes live again — otherwise it hears her own reply
                    # through the speakers and treats that as a new command.
                    speak_thread.join(timeout=30.0)
                    time.sleep(0.4)  # let room echo/reverb tail off
                continue

            # silent / tts modes: typed input
            _voice_mod.VOICE_ENABLED = (mode == "tts")
            try:
                user_input = prompt_session.prompt("\nYou › ", multiline=False)
            except KeyboardInterrupt:
                console.print("\n[dim]Use /exit to quit.[/dim]")
                continue
            except EOFError:
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Slash commands
            if user_input.startswith("/"):
                parts = user_input.split(None, 1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd in ("/exit", "/quit"):
                    break
                elif cmd == "/help":
                    show_help()
                elif cmd == "/memory":
                    show_memory()
                elif cmd == "/history":
                    show_history(messages)
                elif cmd == "/recall":
                    if arg:
                        handle_recall(arg)
                    else:
                        console.print("[dim]Usage: /recall <query>[/dim]")
                elif cmd == "/clear":
                    messages = []
                    system = build_system_prompt(voice_mode=(mode == "listen"))
                    console.print("[dim]Conversation cleared. Memory preserved.[/dim]")
                elif cmd == "/listen":
                    mode = "listen"
                    system = build_system_prompt(voice_mode=True)
                    console.print("[dim]Switched to listen mode (mic input, spoken replies).[/dim]")
                elif cmd == "/silent":
                    mode = "silent"
                    system = build_system_prompt(voice_mode=False)
                    console.print("[dim]Switched to silent mode (type, read replies, no voice).[/dim]")
                elif cmd == "/tts":
                    mode = "tts"
                    system = build_system_prompt(voice_mode=False)
                    console.print("[dim]Switched to tts mode (type your input, Bella speaks replies).[/dim]")
                else:
                    console.print(f"[dim red]Unknown command: {cmd}. Type /help.[/dim red]")
                continue

            messages, _speak_thread = _handle_input(user_input, messages, system, session_db)
            # (silent/tts modes prompt for the next turn's input from the
            # keyboard, not the mic, so there's no feedback risk here —
            # the reply can keep speaking in the background undisturbed.)

    except KeyboardInterrupt:
        pass
    finally:
        console.print("\n[dim]Saving session...[/dim]")
        _join_bg()
        session_db.close()
        console.print("[dim]Goodbye.[/dim]")


def _extract_and_save_memory(user_input: str, reply_text: str) -> None:
    """
    After each turn, run a quick extraction pass to pull out facts worth
    persisting. Fires a small focused prompt — silent, no output to user.
    """
    import re as _re
    import ollama as _ollama
    from memory.core import memory_add, _read_entries, MEMORY_FILE, USER_FILE
    from llm.ollama_client import NO_THINK

    # Not enough signal in a tiny exchange — skip the LLM call entirely.
    if len(user_input.strip()) + len(reply_text.strip()) < 40:
        return
    if reply_text.lstrip().startswith("[") and "error" in reply_text[:40].lower():
        return

    def _norm(s: str) -> str:
        return _re.sub(r"\s+", " ", s).strip().lower().rstrip(".")

    mem_entries  = _read_entries(MEMORY_FILE)
    user_entries = _read_entries(USER_FILE)
    existing_norm = {_norm(e) for e in mem_entries + user_entries}
    existing = "\n".join(mem_entries + user_entries)

    extraction_prompt = f"""You are a strict memory curator. Extract ONLY durable facts the user stated about themselves or their environment in THIS exchange.

Hard rules:
- Extract only facts explicitly present in the conversation below. Never guess or invent.
- "user" store: the user's name, timezone, location, stated preferences, communication style.
- "memory" store: environment/setup facts, project context, conventions, tasks the user confirmed done.
- Do NOT save: greetings, small talk, questions, one-off requests, anything already listed under "Existing memory", anything you are not certain about.
- Default to NOTHING. Most exchanges should produce NOTHING.

Existing memory (never re-save these or close variants):
{existing if existing else "(empty)"}

Conversation:
User: {user_input}
Assistant: {reply_text}

Output one compact JSON object per NEW fact, one per line: {{"target": "user"|"memory", "content": "the fact"}}
If there is nothing new and durable, output exactly: NOTHING"""

    try:
        # Reuse the main loop's num_ctx — a different value makes Ollama drop and
        # rebuild the model KV cache, adding seconds to the next turn.
        chat_kwargs = dict(
            model=MODEL,
            messages=[{"role": "user", "content": extraction_prompt}],
            stream=False,
            options={**BASE_OPTIONS, "num_predict": 256, "temperature": 0},
        )
        try:
            resp = _ollama.chat(think=not NO_THINK, **chat_kwargs)
        except (_ollama.ResponseError, TypeError):
            resp = _ollama.chat(**chat_kwargs)
        raw = resp.get("message", {}).get("content", "").strip()
        if not raw or raw.upper() == "NOTHING":
            return

        import json as _json
        for line in raw.splitlines():
            line = _re.sub(r'^```[a-z]*|```$', '', line.strip()).strip()
            if not line or line.upper() == "NOTHING":
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            target  = obj.get("target", "memory")
            content = (obj.get("content") or "").strip()
            if target not in ("user", "memory") or len(content) < 3:
                continue
            if _norm(content) in existing_norm:
                continue  # model ignored the "don't re-save" rule — enforce it
            result = memory_add(target, content)
            if result.get("success"):
                existing_norm.add(_norm(content))
                console.print(f"  [dim green]🧠 remembered → {target}: {content[:60]}[/dim green]")
    except Exception:
        pass  # Memory extraction is best-effort; never crash the main loop


def _trim_messages(messages: list[dict], max_turns: int = 10) -> list[dict]:
    """
    Keep only the last max_turns pairs (user+assistant) to limit context size.
    Always keeps tool result messages attached to their turn.
    """
    # Each "turn" is roughly 2 messages (user + assistant), plus any tool results
    # Simple approach: keep last max_turns*2 messages
    cutoff = max_turns * 2
    if len(messages) <= cutoff:
        return messages
    return messages[-cutoff:]


# ── Deterministic media controls (Spotify transport + system volume) ─────────
#
# qwen3:8b routinely fabricates "Playback is now paused" / "Volume set to 40"
# without emitting any tool call (and "stop" was never a valid spotify action).
# Bare control phrases are unambiguous, so we run them directly and skip the LLM
# round-trip entirely. Only whole-string matches count — "play <song name>" or
# "set volume to whatever feels right" fall through to the model.

_SPOTIFY_FAST_RULES: list[tuple[str, str]] = [
    (r"(pause|hold|freeze)( it| this| that| everything|"
     r" (the )?(music|song|track|playback|audio)| spotify| playing)?", "pause"),
    (r"(stop|kill|end|halt)( it| this| that|"
     r" (the )?(music|song|track|playback|audio)| spotify| playing)?", "pause"),
    (r"(resume|unpause|continue|keep playing|carry on|play on|un-?pause)"
     r"( it| this| the (music|song|track|playback)| playing| spotify)?", "play"),
    (r"(play|start|unpause)( it| the music| music| the song| the track|"
     r" again| spotify| something)?", "play"),
    (r"(next|skip|forward)( it| this| song| track| the song| this song|"
     r" this track| ahead| forward| please)?", "next"),
    (r"(previous|prev|go back|back a( song| track| bit)?|last song|"
     r"previous song|previous track|rewind)( song| track| one| please)?", "previous"),
    (r"(what('?s| is| ?)( currently| now)? playing( right now| now| atm)?"
     r"|(what|which) (song|track|tune)( is|'?s)?"
     r" (this|that|playing|on|being played|currently playing|is being played)"
     r"( right now| now| atm)?"
     r"|(the )?current (song|track|tune)"
     r"|now playing"
     r"|what is this (song|track)"
     r"|what('?s| is) (this|the) (song|track)( called| named)?"
     r"|what am i listening to"
     r"|name of (this|the current|the) (song|track)"
     r"|song name"
     r"|who('?s| is)( this)? (playing|singing( this)?)"
     r"|who sings this)", "current"),
]

_VOL_OBJ = r"( it| the (volume|sound|audio|music))?"
_VOL_HEDGE = r"( a bit| a little| a touch| some| please)?"

_VOL_UP = (r"(?:increase|raise|boost|bump|crank|"
           r"turn\s+(?:it\s+|the\s+volume\s+|the\s+sound\s+)?up|turn\s+up)")
_VOL_DOWN = (r"(?:decrease|lower|reduce|drop|cut|"
             r"turn\s+(?:it\s+|the\s+volume\s+|the\s+sound\s+)?down|turn\s+down|"
             r"knock\s+(?:it\s+)?down|bring\s+(?:it\s+)?down)")
# object + "by N" + optional unit; bare number = points (macOS scale), % = relative
_VOL_BY = (r"(?:\s+it|\s+(?:the\s+)?volume|\s+(?:the\s+)?sound|\s+(?:the\s+)?audio)?"
           r"\s+by\s+(\d{1,3})\s*(%|percent|points?|pts?)?")


def _vol_by(match: "re.Match", sign: int) -> dict:
    n = int(match.group(1))
    unit = match.group(2) or ""
    if "%" in unit or "percent" in unit:
        return {"action": "adjust", "percent": sign * n}
    return {"action": "adjust", "delta": sign * n}


def _fast_volume_input(t: str) -> dict | None:
    if re.fullmatch(rf"(mute|silence){_VOL_OBJ}", t):
        return {"action": "mute"}
    if re.fullmatch(rf"un-?mute{_VOL_OBJ}", t):
        return {"action": "unmute"}
    if re.fullmatch(
        r"(what('?s| is)( the)?( current)? volume|current volume|"
        r"volume level|how loud is it)", t):
        return {"action": "get"}

    # Relative change by an explicit amount: "lower the volume by 20%", "turn it up by 10"
    m = re.fullmatch(_VOL_UP + _VOL_BY, t)
    if m:
        return _vol_by(m, +1)
    m = re.fullmatch(_VOL_DOWN + _VOL_BY, t)
    if m:
        return _vol_by(m, -1)
    if re.fullmatch(r"(halve|cut in half)( the (volume|sound))?|cut the (volume|sound) in half", t):
        return {"action": "adjust", "percent": -50}
    if re.fullmatch(r"double( the (volume|sound))?", t):
        return {"action": "adjust", "percent": 100}

    m = re.fullmatch(
        r"(?:set |change |make |put )?(?:the )?volume(?: level)? "
        r"(?:to |at |= ?)?(\d{1,3})(?: ?%| ?percent)?", t)
    if m:
        return {"action": "set", "level": max(0, min(100, int(m.group(1))))}
    if re.fullmatch(r"(?:set |put )?(?:the )?volume (?:to )?(?:max|maximum|full|all the way up)", t):
        return {"action": "set", "level": 100}
    if re.fullmatch(r"(?:set |put )?(?:the )?volume (?:to )?(?:min|minimum|zero)", t):
        return {"action": "set", "level": 0}
    if re.fullmatch(r"max(?:imum)? volume|full volume|full blast", t):
        return {"action": "set", "level": 100}

    if re.fullmatch(
        rf"(turn (it |the volume |the sound )?up|turn up( the volume| the sound| it)?|"
        rf"volume up|louder|crank it( up)?|bump it up|pump it up|more volume|"
        rf"increase (the )?volume|up{_VOL_OBJ}){_VOL_HEDGE}", t):
        return {"action": "adjust", "delta": 10}
    if re.fullmatch(
        rf"(turn (it |the volume |the sound )?down|turn down( the volume| the sound| it)?|"
        rf"volume down|quieter|softer|lower (the )?volume|less volume|"
        rf"decrease (the )?volume|down{_VOL_OBJ}){_VOL_HEDGE}", t):
        return {"action": "adjust", "delta": -10}
    if re.fullmatch(r"(way|much|a lot) (louder|higher|up)", t):
        return {"action": "adjust", "delta": 25}
    if re.fullmatch(r"(way|much|a lot) (quieter|lower|softer|down)", t):
        return {"action": "adjust", "delta": -25}
    return None


# First-word vocabulary for typo repair ("increayse by 10%" → "increase by 10%").
# Only the leading token is corrected, and a full pattern match is still required
# afterward, so a stray fix that doesn't form a real command falls through.
_MEDIA_VERB_VOCAB = (
    "increase decrease raise lower reduce boost bump crank drop cut halve double "
    "turn set make put mute unmute silence volume louder quieter softer "
    "pause stop hold resume unpause continue play start next skip forward "
    "previous prev rewind"
).split()


def _damerau_levenshtein(a: str, b: str) -> int:
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def _repair_first_word(t: str) -> str:
    words = t.split()
    if not words or words[0] in _MEDIA_VERB_VOCAB or not words[0].isalpha():
        return t
    w = words[0]
    budget = 1 if len(w) <= 6 else 2
    best, best_d = None, budget + 1
    for cand in _MEDIA_VERB_VOCAB:
        if cand[0] != w[0] or abs(len(cand) - len(w)) > budget:
            continue
        d = _damerau_levenshtein(w, cand)
        if d < best_d:
            best, best_d = cand, d
    if best is not None and best_d <= budget:
        words[0] = best
        return " ".join(words)
    return t


# "play <song>" — the model routinely fabricates a plausible track name here
# ("Playing "Back in Black — AC/DC".") without ever calling the tool. Run the
# search+play directly so the reply reflects what Spotify actually did.
_PLAY_NOT_A_QUERY = {
    "music", "the music", "it", "some music", "the song", "the track",
    "again", "spotify", "something", "some", "on", "it again", "that again",
    "this again", "the next song", "next song", "the previous song",
}


def _fast_play_query(t: str) -> str | None:
    m = re.fullmatch(
        r"(?:play(?: me| us)?|(?:put|throw|chuck) on some|start playing)\s+(.+)", t)
    if not m:
        return None
    q = re.sub(r"\s+(on spotify|please|now|for me)$", "", m.group(1)).strip()
    if not q or q in _PLAY_NOT_A_QUERY:
        return None
    if re.fullmatch(r"(the )?(next|previous|last|first) (song|track|one)", q):
        return None
    return q


_DAY_WORD = r"(?:today|tonight|tomorrow)"

_RELATIVE_TIME_RE = re.compile(
    r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
_NOON_MIDNIGHT_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*\b(noon|midnight)\b\s*({_DAY_WORD})?\b", re.IGNORECASE)
_CLOCK_AMPM_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*(?:\bat\b\s*)?(\d{{1,2}})(?::(\d{{2}}))?\s*([ap]\.?m\.?)\s*({_DAY_WORD})?\b",
    re.IGNORECASE)
_CLOCK_24H_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*(?:\bat\b\s*)?([01]?\d|2[0-3]):([0-5]\d)\s*({_DAY_WORD})?\b",
    re.IGNORECASE)


def _roll_day(dt, day_word: str | None, now):
    """Apply an explicit today/tonight/tomorrow qualifier, else roll to
    tomorrow only if the bare time has already passed today."""
    from datetime import timedelta
    day_word = (day_word or "").lower()
    if day_word == "tomorrow":
        return dt + timedelta(days=1)
    if day_word in ("today", "tonight"):
        return dt
    if dt <= now:
        return dt + timedelta(days=1)
    return dt


def _parse_reminder_time(text: str, now):
    """Find one unambiguous time expression in `text`. Returns
    (due_datetime_or_None, text_with_the_time_expression_removed).
    Deliberately conservative: only fires on an explicit clock time,
    'noon'/'midnight', or 'in N minutes/hours' — never guesses."""
    from datetime import timedelta

    m = _RELATIVE_TIME_RE.search(text)
    if m:
        n = int(m.group(1))
        mins = n * 60 if m.group(2).lower().startswith("h") else n
        dt = now + timedelta(minutes=mins)
        return dt, (text[:m.start()] + text[m.end():])

    m = _NOON_MIDNIGHT_RE.search(text)
    if m:
        hour = 12 if m.group(2).lower() == "noon" else 0
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(3), now)
        return dt, (text[:m.start()] + text[m.end():])

    m = _CLOCK_AMPM_RE.search(text)
    if m:
        hour = int(m.group(2)) % 12
        minute = int(m.group(3) or 0)
        if m.group(4).lower().replace(".", "") == "pm":
            hour += 12
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(5), now)
        return dt, (text[:m.start()] + text[m.end():])

    m = _CLOCK_24H_RE.search(text)
    if m:
        dt = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(4), now)
        return dt, (text[:m.start()] + text[m.end():])

    return None, text


_REMINDER_CONNECTOR_RE = re.compile(
    r"^(to|about|for|on|at|that says|saying|titled|called)\s+", re.IGNORECASE)
_AMBIGUOUS_BARE_NUM_RE = re.compile(r"\b(?:at|by)\s+\d{1,2}\b", re.IGNORECASE)
_REMINDER_STOPWORDS = {
    "to", "about", "for", "on", "at", "that", "saying", "titled", "called", "it", "this",
}


def _fast_reminder_call(text: str) -> tuple[str, dict] | None:
    """Handle the common, unambiguous 'remind me...'/'set a reminder...' add
    phrasing without going through the LLM at all — the 8B model has a
    documented history of fabricating success or using the wrong tool here.
    Falls through to the normal LLM path (returns None) for anything it
    can't parse with confidence, so it only ever adds reliability, never
    blocks a request."""
    t = text.strip()
    t = re.sub(r"^(hey |ok |okay )?(bella|jarvis)[\s,:]+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(please|can you|could you|would you)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\s.!?,;]+$", "", t).strip()

    m = re.match(r"^remind me\s+(?:to\s+)?(.+)$", t, flags=re.IGNORECASE)
    if not m:
        m = re.match(
            r"^(?:set|add|create)\s+(?:a|an)?\s*reminders?\s*"
            r"(?:to|about|for|that says|saying|titled|called)?\s*(.+)$",
            t, flags=re.IGNORECASE,
        )
    if not m:
        return None

    rest = m.group(1).strip()
    if not rest:
        return None

    now = datetime.now()
    due_dt, remaining = _parse_reminder_time(rest, now)

    if due_dt is None and re.search(rf"\b{_DAY_WORD}\b", rest, flags=re.IGNORECASE):
        # A bare day-word with no clock time ("remind me tomorrow to...") is
        # ambiguous about when — defer to the LLM rather than mis-title it.
        return None
    if due_dt is None and _AMBIGUOUS_BARE_NUM_RE.search(rest):
        # "at 8" / "by 5" with no am/pm or colon — could be a time we failed
        # to parse; don't risk swallowing it into the title.
        return None

    remaining = remaining.strip(" ,.")
    remaining = _REMINDER_CONNECTOR_RE.sub("", remaining)
    remaining = re.sub(r"\s{2,}", " ", remaining).strip(" ,.")
    if not remaining or remaining.lower() in _REMINDER_STOPWORDS:
        return None

    inp = {"action": "add", "text": remaining}
    if due_dt:
        inp["due_date"] = due_dt.strftime("%Y-%m-%d %H:%M")
    return "reminder", inp


def _fast_media_call(text: str) -> tuple[str, dict] | None:
    """Return (tool_name, tool_input) for an unambiguous media command, else None."""
    t = text.strip().lower()
    t = re.sub(r"^(hey |ok |okay )?(bella|jarvis)[\s,:]+", "", t)
    t = re.sub(r"^(please|can you|could you|would you)\s+", "", t)
    t = re.sub(r"[\s.!?,;]+$", "", t).strip()
    t = _repair_first_word(t)

    for pattern, action in _SPOTIFY_FAST_RULES:
        if re.fullmatch(pattern, t):
            return "spotify", {"action": action}

    query = _fast_play_query(t)
    if query:
        return "spotify", {"action": "play", "query": query}

    vol = _fast_volume_input(t)
    if vol:
        return "volume", vol
    return None


def _fast_media_reply(tool: str, inp: dict, result: dict) -> str:
    if not result.get("success"):
        err = (result.get("error") or result.get("stderr")
               or f"{tool} didn't respond.")
        return f"Couldn't do that — {err}"

    if tool == "reminder":
        action = inp["action"]
        if action == "add":
            due = result.get("due_date")
            when = f" for {due}" if due else ""
            return f'Reminder added: "{result.get("added", inp.get("text", ""))}"{when}.'
        return "Done."

    if tool == "spotify":
        action = inp["action"]
        track = (result.get("track") or "").strip(" —")
        state = result.get("player_state", "")
        if action == "pause":
            return "Paused." if state != "playing" else "Sent pause, but Spotify still shows playing."
        if action == "play":
            if not track:
                return "Playing."
            if state and state != "playing":
                return f"Loaded {track}, but Spotify shows it {state}."
            return f"Now playing {track}."
        if action in ("next", "previous"):
            return f"{'Skipped' if action == 'next' else 'Went back'}{f' — now playing {track}' if track else '.'}"
        if action == "current":
            if not track:
                return "Nothing's playing."
            return track if state == "playing" else f"{track} ({state or 'stopped'})"
        return "Done."

    # volume
    action = inp["action"]
    vol = result.get("volume")
    muted = result.get("muted")
    tag = " (muted)" if muted else ""
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return f"Unmuted — volume {vol}."
    if action == "get":
        return f"Volume is {vol}{tag}."
    prev = result.get("previous_volume")
    if action == "adjust" and prev is not None:
        return f"Volume {prev} → {vol}{tag}."
    return f"Volume set to {vol}{tag}."


def _handle_fast_media(
    tool: str, inp: dict, user_input: str, messages: list[dict], session_db: Session,
):
    """Returns (updated_messages, speak_thread_or_None). The caller (listen
    mode) needs that thread to wait for Bella to actually finish talking
    before opening the mic again — see the note on _handle_input."""
    session_db.add_message("user", user_input)
    on_tool_start(tool, inp)
    result_str = execute_tool(tool, inp)
    on_tool_end(tool, result_str)
    try:
        result = json.loads(result_str)
    except Exception:
        result = {"raw": result_str}

    reply = _fast_media_reply(tool, inp, result)
    console.print(f"[bold cyan]Bella[/bold cyan] {reply}")
    speak_thread = _spawn_bg(speak, reply)  # trial voice feature — no-op unless JARVIS_VOICE=1

    session_db.add_message("assistant", reply)
    new_messages = messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": reply},
    ]
    return new_messages, speak_thread


def _handle_input(
    user_input: str,
    messages: list[dict],
    system: str,
    session_db: Session,
):
    """Returns (updated_messages, speak_thread_or_None).

    The speak thread is returned (not just fire-and-forgotten) so listen
    mode can join it before recording again — otherwise the mic starts
    listening while Bella's own reply is still playing through the
    speakers, picks up her voice as new "speech", and transcribes/acts on
    it — a feedback loop talking to itself."""
    fast = _fast_media_call(user_input) or _fast_reminder_call(user_input)
    if fast:
        return _handle_fast_media(*fast, user_input, messages, session_db)

    session_db.add_message("user", user_input)
    messages = messages + [{"role": "user", "content": user_input}]

    spinner = _Spinner("thinking...")
    spinner.start()
    started_reply = {"done": False}

    def on_text(t: str):
        spinner.pause()
        if not started_reply["done"]:
            started_reply["done"] = True
            spinner.write("\033[1;36mBella\033[0m ")
        spinner.write(t)

    def on_tool_start_ui(name: str, inputs: dict):
        spinner.pause()
        on_tool_start(name, inputs)
        spinner.resume(f"running {name}…")

    def on_tool_end_ui(name: str, result_str: str):
        spinner.pause()
        on_tool_end(name, result_str)
        spinner.resume("thinking...")

    reply_text, _ = run_agent_turn(
        messages=_trim_messages(messages),
        system=system,
        on_text=on_text,
        on_tool_start=on_tool_start_ui,
        on_tool_end=on_tool_end_ui,
    )

    spinner.stop()

    sys.stdout.write("\n")
    sys.stdout.flush()

    if not reply_text:
        # Model produced nothing usable (e.g. an Ollama error). Drop the
        # unanswered user message so history stays clean user/assistant pairs.
        return messages[:-1], None

    session_db.add_message("assistant", reply_text)
    _spawn_bg(_extract_and_save_memory, user_input, reply_text)
    speak_thread = _spawn_bg(speak, reply_text)  # trial voice feature — no-op unless JARVIS_VOICE=1

    # Append to full untruncated history for next turn
    return messages + [{"role": "assistant", "content": reply_text}], speak_thread


if __name__ == "__main__":
    single_shot = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    run_repl(single_shot=single_shot)
