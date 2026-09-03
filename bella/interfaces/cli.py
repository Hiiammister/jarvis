"""
bella.interfaces.cli — the text REPL, listen loop, single-shot mode.

Thin orchestration only: read input, dispatch slash commands, drive one
`BellaRuntime` turn per user message. All rendering goes through
`bella.interfaces.renderer.Renderer`; all conversation logic through the
runtime. Nothing here calls `console.print` or emits ANSI directly.

    bella / bella chat   -> run_repl()          (interactive)
    bella voice           -> run_repl(start_mode="listen")
    bella "do X"          -> run_single_shot()   (one turn, no mic, exit)
"""

from __future__ import annotations

import time

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from bella.bootstrap import BootstrapReport, bootstrap
from bella.config import get_config
from bella.interfaces import theme
from bella.interfaces import voice as voice_iface
from bella.interfaces.renderer import Renderer
from bella.interfaces.spinner import Spinner
from bella.runtime import BellaRuntime
from tools.voice import speak

PROMPT_STYLE = Style.from_dict({"you": f"{theme.USER_COLOR} bold", "caret": theme.ACCENT_COLOR})
USER_PROMPT = FormattedText([("class:you", "You "), ("class:caret", "› ")])

_MODE_SLASH = {"/listen": "listen", "/silent": "silent", "/tts": "tts"}


# ── one turn ──────────────────────────────────────────────────────────────

def _do_turn(renderer: Renderer, runtime: BellaRuntime, user_input: str,
             *, voice_mode: bool, speaking: bool, timings=None):
    """Run one turn. Returns the speak thread (or None) so listen mode can wait
    for Bella to finish talking before the mic reopens. If `timings` is passed
    in (listen mode, so STT/TTS spans join the same block), the caller renders
    it; otherwise this renders it here."""
    from bella.latency import Timings

    own = timings is None
    if own and renderer.verbose:
        timings = Timings()

    with renderer.turn(speaking=speaking) as view:
        result = runtime.handle(
            user_input,
            voice_mode=voice_mode,
            on_text=view.on_text,
            on_tool_start=view.on_tool_start,
            on_tool_end=view.on_tool_end,
            on_memory_note=view.on_memory_note,
            timings=timings,
        )
        view.finish(result)

    if own:
        renderer.latency(timings)

    if result.reply:
        return runtime.spawn_bg(speak, result.reply, timings)  # speak() no-ops unless voice output on
    return None


# ── single shot ────────────────────────────────────────────────────────────

def run_single_shot(request: str, runtime: BellaRuntime | None = None,
                    verbose: bool | None = None) -> None:
    """`bella "do X"` — one turn, no mic, then exit."""
    config = get_config()
    owns = runtime is None
    runtime = runtime or BellaRuntime(config=config)
    renderer = Renderer(verbose=config.verbose if verbose is None else verbose)
    voice_iface.set_voice_output(False)
    try:
        _do_turn(renderer, runtime, request, voice_mode=False, speaking=False)
    finally:
        if owns:
            runtime.shutdown()


# ── REPL ──────────────────────────────────────────────────────────────────

def run_repl(runtime: BellaRuntime | None = None, report: BootstrapReport | None = None,
             start_mode: str = "listen", verbose: bool | None = None) -> None:
    config = get_config()
    report = report or bootstrap(config)
    owns = runtime is None
    runtime = runtime or BellaRuntime(config=config)
    renderer = Renderer(verbose=config.verbose if verbose is None else verbose)

    mode = start_mode
    forced_silent = False
    if start_mode == "listen" and not report.voice_input_available:
        mode = "silent"
        forced_silent = True

    renderer.startup(report, mode)
    if forced_silent:
        renderer.notice("Voice input unavailable — starting in text mode. /listen to retry.")

    history_file = config.history_file
    history_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_session = PromptSession(history=FileHistory(str(history_file)), style=PROMPT_STYLE)

    try:
        while True:
            if mode == "listen":
                mode = _listen_turn(renderer, runtime, mode)
                if mode == "exit":
                    break
                continue

            voice_iface.set_voice_output(mode == "tts")
            try:
                user_input = prompt_session.prompt(USER_PROMPT, multiline=False).strip()
            except KeyboardInterrupt:
                renderer.notice("Use /exit to quit.")
                continue
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.startswith("/"):
                outcome = _handle_slash(user_input, renderer, runtime, report, mode)
                if outcome == "exit":
                    break
                mode = _MODE_SLASH.get(user_input.split(None, 1)[0].lower(), mode)
                continue

            _do_turn(renderer, runtime, user_input, voice_mode=False, speaking=(mode == "tts"))

    except KeyboardInterrupt:
        pass
    finally:
        if owns:
            runtime.shutdown()
        renderer.goodbye()


def _listen_turn(renderer: Renderer, runtime: BellaRuntime, mode: str) -> str:
    """One iteration of listen mode. Returns the (possibly changed) mode."""
    from bella.latency import Timings

    voice_iface.set_voice_output(True)
    renderer.listen_idle()
    tm = Timings() if renderer.verbose else None

    try:
        heard, typed = voice_iface.wait_for_wake_or_enter()
    except KeyboardInterrupt:
        renderer.notice('Use /exit to quit, or say "silent mode" to type instead.')
        return mode

    command: str | None = None
    if typed is not None:
        if typed.strip():
            command = typed.strip()
            if command.startswith("/"):
                if _handle_slash(command, renderer, runtime, None, mode) == "exit":
                    return "exit"
                return _MODE_SLASH.get(command.split(None, 1)[0].lower(), mode)
        else:
            renderer.listen_prompt("listening…")
            command = voice_iface.capture_command(_listening_spinner, timings=tm)
    else:
        if not heard:
            return mode  # ambient noise
        addressed, command = voice_iface.strip_wake_word(heard)
        if not addressed:
            renderer.listen_miss(f'(not addressed to Bella: "{heard[:60]}")')
            return mode
        if not command:
            renderer.listen_prompt("yes?")
            command = voice_iface.capture_command(_listening_spinner, timings=tm)

    if not command:
        renderer.listen_miss("(didn't catch a command)")
        return mode

    switched = voice_iface.match_mode_phrase(command)
    if switched:
        runtime._system_cache.clear()
        renderer.mode_switch(switched)
        return switched

    renderer.user_echo(command)
    speak_thread = _do_turn(renderer, runtime, command, voice_mode=True, speaking=True, timings=tm)
    if speak_thread is not None:
        # Wait for Bella to finish talking before the mic reopens — otherwise it
        # hears her own reply through the speakers and treats it as a command.
        speak_thread.join(timeout=30.0)
        time.sleep(0.4)  # let room echo tail off
    renderer.latency(tm)
    return mode


def _listening_spinner() -> Spinner:
    return Spinner("listening", marker=theme.MIC, marker_style=theme.ANSI_ACCENT, who="")


# ── slash commands ───────────────────────────────────────────────────────

def _handle_slash(user_input: str, renderer: Renderer, runtime: BellaRuntime,
                  report: BootstrapReport | None, mode: str) -> str | None:
    parts = user_input.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    config = runtime.config

    if cmd in ("/exit", "/quit"):
        return "exit"

    if cmd == "/help":
        renderer.help()
    elif cmd == "/status":
        from bella.doctor import server_running

        rep = report or bootstrap(config)
        renderer.status(config=config, report=rep, mode=mode, server_running=server_running(config))
    elif cmd == "/doctor":
        from bella.doctor import run_diagnostics

        renderer.doctor(run_diagnostics(config))
    elif cmd == "/tools":
        from tools.registry import get_registry

        renderer.tools(get_registry())
    elif cmd == "/memory":
        renderer.memory_dump()
    elif cmd == "/history":
        renderer.history_dump(runtime.messages)
    elif cmd == "/recall":
        if not arg:
            renderer.notice("Usage: /recall <query>")
        else:
            from memory.core import search_history

            renderer.recall_dump(arg, search_history(arg, limit=5))
    elif cmd == "/verbose":
        renderer.verbose_toggle()
    elif cmd == "/benchmark":
        from bella.benchmark import run_benchmark

        renderer.notice("Running latency benchmark…")
        renderer.benchmark(run_benchmark(config))
    elif cmd == "/clear":
        runtime.reset_conversation()
        renderer.notice("Context cleared. Memory preserved.")
    elif cmd in _MODE_SLASH:
        runtime._system_cache.clear()
        renderer.mode_switch(_MODE_SLASH[cmd])
    else:
        renderer.notice(f"Unknown command: {cmd} — /help for the list")
    return None
