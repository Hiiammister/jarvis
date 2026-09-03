"""
bella.interfaces.renderer — every line the CLI prints goes through here.

The renderer owns the Rich `Console` and turns raw events (a streamed token, a
tool starting, a diagnostics result) into the restrained house style defined in
`theme.py`. `cli.py` never calls `console.print` directly.

Two visible objects:
  Renderer   — startup, prompts, status/doctor/help/tools, notices, errors
  TurnView   — one agent turn: spinner + streamed reply + tool lines
               (`with renderer.turn(...) as view:` ...)

Verbose mode (`--verbose` / `/verbose` / BELLA_VERBOSE) adds raw tool names,
arguments, timings, and raw tool output. Normal mode stays quiet.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from bella.interfaces import theme
from bella.interfaces.spinner import Spinner


class Renderer:
    def __init__(self, console: Console | None = None, verbose: bool = False):
        self.console = console or Console(theme=theme.RICH_THEME, highlight=False, soft_wrap=False)
        self.verbose = verbose
        self._listen_hint_shown = False

    # ── startup ────────────────────────────────────────────────────────────

    def startup(self, report, mode: str) -> None:
        c = self.console
        c.print()
        for row, colour in zip(theme.LOGO.splitlines(), theme.LOGO_GRADIENT):
            c.print(f"[bold {colour}]{row}[/]", highlight=False, no_wrap=True, crop=False)
        c.print()
        c.print(f"[muted]{theme.TAGLINE}[/]")
        c.print()

        chips = [report.model, "local", f"{report.tool_count} tools"]
        chips.append("memory ready" if report.memory_ok else "memory unavailable")
        if report.voice_input_available or report.voice_output_available:
            chips.append("voice ready")
        c.print("[muted]" + "  ·  ".join(escape(x) for x in chips) + "[/]")

        warnings = self._startup_warnings(report)
        for w in warnings[:3]:
            c.print(f"[warning]{theme.WARN} {escape(w)}[/]")
        if len(warnings) > 3:
            c.print(f"[muted]…and {len(warnings) - 3} more — see /doctor[/]")

        c.print()
        if mode == "listen":
            c.print(f'[accent]{theme.MIC}[/] [response]Say “Bella” or press Enter to talk[/]')
            c.print("[muted]   Type /help for commands[/]")
        else:
            c.print("[muted]Type a message, or /help for commands[/]")
        c.print()

    @staticmethod
    def _startup_warnings(report) -> list[str]:
        out = [f"{name} unavailable" for name, _ in report.tools_unavailable]
        if not report.voice_input_available:
            out.append("microphone unavailable" if report.voice_output_available else "voice unavailable")
        return out

    # ── conversation lines ────────────────────────────────────────────────

    def user_echo(self, text: str) -> None:
        """Listen mode: show the transcribed command as if typed."""
        self.console.print(f"\n[user]You[/] [accent]{theme.CARET}[/] [response]{escape(text)}[/]")

    def bella_reply(self, text: str, *, speaking: bool = False) -> None:
        """A complete (non-streamed) reply — the fast path."""
        marker = theme.SPEAK if speaking else f"[accent]{theme.CARET}[/]"
        self.console.print(f"[bella]Bella[/] {marker} [response]{escape(text)}[/]")

    def memory_note(self, target: str, content: str) -> None:
        if self.verbose:
            self.console.print(f"  [muted]· remembered → {target}: {escape(content[:80])}[/]")
        else:
            self.console.print(f"  [muted]· remembered ({target})[/]")

    # ── mode / status one-liners ─────────────────────────────────────────

    def mode_switch(self, mode: str) -> None:
        self.console.print(f"[muted]Mode →[/] [accent]{mode}[/]")
        if mode == "listen":
            self._listen_hint_shown = False

    def verbose_toggle(self) -> None:
        self.verbose = not self.verbose
        state = "on" if self.verbose else "off"
        self.console.print(f"[muted]Verbose →[/] [accent]{state}[/]")

    def notice(self, text: str) -> None:
        self.console.print(f"[muted]{escape(text)}[/]")

    def route_note(self, result) -> None:
        """Verbose only: which path handled the turn."""
        if not self.verbose or not getattr(result, "route", ""):
            return
        extra = f" · {result.profile}" if getattr(result, "profile", "") else ""
        self.console.print(f"[muted]· route {result.route}{extra}[/]")

    def latency(self, timings) -> None:
        """Verbose only: the [latency] block."""
        if not self.verbose or timings is None:
            return
        rows = timings.as_rows()
        if not rows:
            return
        c = self.console
        width = max(len(k) for k, _ in rows)
        c.print("\n[muted][latency][/]")
        for k, v in rows:
            c.print(f"[muted]  {k.ljust(width)}   {timings.fmt(v)}[/]")

    def benchmark(self, rows: list[tuple[str, str, str]]) -> None:
        c = self.console
        c.print("\n[heading]BELLA LATENCY[/]\n")
        width = max((len(label) for label, _, _ in rows), default=0)
        for label, value, detail in rows:
            note = f"   [muted]{escape(detail)}[/]" if detail else ""
            c.print(f"  [response]{label.ljust(width)}[/]   [accent]{value}[/]{note}")
        c.print()

    def error(self, title: str, detail: str | None = None, extra: list[str] | None = None) -> None:
        self.console.print(f"[error]{theme.FAIL} {escape(title)}[/]")
        if detail:
            self.console.print(f"  [muted]{escape(detail)}[/]")
        for line in extra or []:
            self.console.print(f"  [muted]{escape(line)}[/]")

    def goodbye(self, err: str | None = None) -> None:
        if err:
            self.console.print(f"\n[error]{theme.FAIL} {escape(err)}[/]")
            return
        self.console.print(f"\n[bella]Bella[/] [accent]{theme.CARET}[/] [response]See you.[/]")

    # ── listen-mode idle prompt ──────────────────────────────────────────

    def listen_idle(self) -> None:
        if not self._listen_hint_shown:
            self._listen_hint_shown = True
            self.console.print(f'\n[accent]{theme.MIC}[/] [response]Say “Bella” or press Enter…[/]')
        else:
            self.console.print(f"\n[accent]{theme.MIC}[/] [muted]listening…[/]")

    def listen_prompt(self, text: str) -> None:
        """A short prompt while actively capturing (post wake-word)."""
        self.console.print(f"[accent]{theme.MIC}[/] [muted]{escape(text)}[/]")

    def listen_miss(self, text: str) -> None:
        self.console.print(f"[muted]{escape(text)}[/]")

    # ── turn ─────────────────────────────────────────────────────────────

    def turn(self, *, speaking: bool = False) -> "TurnView":
        return TurnView(self, speaking=speaking)

    # ── /status ─────────────────────────────────────────────────────────

    def status(self, *, config, report, mode: str, server_running: bool) -> None:
        c = self.console
        voice = report.voice_input_available or report.voice_output_available
        host = "localhost" if config.server_host in ("0.0.0.0", "", "127.0.0.1") else config.server_host
        server = f"{host}:{config.server_port}"
        if not server_running:
            server += "  (not running)"
        rows = [
            ("Model", config.ollama_model),
            ("Provider", "Ollama"),
            ("Memory", "ready" if report.memory_ok else "unavailable"),
            ("Tools", str(report.tool_count)),
            ("Voice", "ready" if voice else "unavailable"),
            ("Mode", mode),
            ("Server", server),
        ]
        w = max(len(k) for k, _ in rows)
        c.print("\n[heading]BELLA STATUS[/]\n")
        for k, v in rows:
            c.print(f"  [muted]{k.ljust(w)}[/]   [response]{escape(str(v))}[/]")
        c.print()

    # ── /doctor ─────────────────────────────────────────────────────────

    _DOCTOR_SYMBOL = {
        "ok": ("success", theme.OK),
        "warn": ("warning", theme.WARN),
        "fail": ("error", theme.FAIL),
        "inactive": ("muted", theme.IDLE),
    }

    def doctor(self, checks: list) -> None:
        c = self.console
        c.print("\n[heading]BELLA DOCTOR[/]")

        groups: dict[str, list] = {}
        for chk in checks:
            groups.setdefault(getattr(chk, "group", "Core"), []).append(chk)

        order = ["Core", "Capabilities", "Server"]
        ordered = [g for g in order if g in groups] + [g for g in groups if g not in order]

        for group in ordered:
            rows = groups[group]
            c.print(f"\n[heading]{group}[/]")
            width = max((len(r.name) for r in rows), default=0)
            for r in rows:
                style, sym = self._DOCTOR_SYMBOL.get(r.status, ("muted", "·"))
                detail = f"   [muted]{escape(r.detail)}[/]" if r.detail else ""
                c.print(f"  [{style}]{sym}[/] [response]{r.name.ljust(width)}[/]{detail}")

        passed = sum(1 for r in checks if r.status == "ok")
        optional = sum(1 for r in checks if r.status in ("warn", "inactive"))
        failed = sum(1 for r in checks if r.status == "fail")
        parts = [f"{passed} passed"]
        if optional:
            parts.append(f"{optional} optional unavailable")
        if failed:
            parts.append(f"{failed} failed")
        c.print(f"\n[muted]{' · '.join(parts)}[/]\n")

    # ── /help ───────────────────────────────────────────────────────────

    def help(self) -> None:
        c = self.console
        sections = [
            ("Conversation", [
                ("/clear", "Clear current context"),
                ("/history", "Show current session"),
                ("/recall", "Search past conversations"),
                ("/memory", "Show persistent memory"),
            ]),
            ("Modes", [
                ("/listen", "Voice input + spoken replies"),
                ("/silent", "Text only"),
                ("/tts", "Typed input + spoken replies"),
            ]),
            ("System", [
                ("/status", "Bella status"),
                ("/doctor", "Diagnostics"),
                ("/tools", "Available tools"),
                ("/benchmark", "Measure path latencies"),
                ("/verbose", "Toggle verbose output + latency"),
            ]),
        ]
        c.print("\n[heading]BELLA COMMANDS[/]")
        for title, rows in sections:
            c.print(f"\n[heading]{title}[/]")
            for cmd, desc in rows:
                c.print(f"  [accent]{cmd.ljust(11)}[/]  [response]{desc}[/]")
        c.print(f"\n  [accent]{'/exit'.ljust(11)}[/]  [response]Exit Bella[/]\n")

    # ── /tools ──────────────────────────────────────────────────────────

    def tools(self, registry) -> None:
        c = self.console
        c.print("\n[heading]BELLA TOOLS[/]\n")
        tools = sorted(registry.all(), key=lambda t: (not t.available, t.name))
        width = max((len(t.name) for t in tools), default=0)
        for t in tools:
            label = theme.activity_label(t.name)
            if t.available:
                c.print(f"  [accent]{t.name.ljust(width)}[/]   [response]{label}[/]")
            else:
                reason = t.unavailable_reason or "unavailable"
                c.print(f"  [muted]{t.name.ljust(width)}   {label}  —  {escape(reason)}[/]")
        c.print()

    # ── /memory · /history · /recall ────────────────────────────────────

    def memory_dump(self) -> None:
        from memory.core import memory_read

        c = self.console
        c.print("\n[heading]PERSISTENT MEMORY[/]")
        for target, label in [("memory", "MEMORY.md"), ("user", "USER.md")]:
            r = memory_read(target)
            c.print(f"\n[heading]{label}[/]  [muted]{r['pct']}% · {r['chars']}/{r['limit']} chars[/]")
            if r["entries"]:
                for i, entry in enumerate(r["entries"], 1):
                    c.print(f"  [muted]{i}.[/] [response]{escape(entry)}[/]")
            else:
                c.print("  [muted](empty)[/]")
        c.print()

    def history_dump(self, messages: list[dict]) -> None:
        c = self.console
        c.print("\n[heading]SESSION HISTORY[/]\n")
        shown = [m for m in messages if not isinstance(m["content"], list)]
        if not shown:
            c.print("[muted]No messages yet.[/]\n")
            return
        for msg in shown:
            who = "Bella" if msg["role"] == "assistant" else "You"
            style = "bella" if msg["role"] == "assistant" else "user"
            body = msg["content"][:500] + ("…" if len(msg["content"]) > 500 else "")
            c.print(f"[{style}]{who}[/] [accent]{theme.CARET}[/] [response]{escape(body)}[/]")
        c.print()

    def recall_dump(self, query: str, results: list[dict]) -> None:
        c = self.console
        c.print(f"\n[heading]RECALL[/]  [muted]{escape(query)}[/]\n")
        if not results:
            c.print("[muted]No matching messages found.[/]\n")
            return
        for r in results:
            who = "Bella" if r["role"] == "assistant" else "You"
            body = r["content"][:300] + ("…" if len(r["content"]) > 300 else "")
            c.print(f"[muted]{r['ts']}[/]  [accent]{who}[/]")
            c.print(f"  [response]{escape(body)}[/]")
        c.print()


class TurnView:
    """The render state for a single agent turn."""

    def __init__(self, renderer: Renderer, *, speaking: bool):
        self.r = renderer
        self.speaking = speaking
        self.spinner = Spinner("thinking")
        self._streamed = False
        self._tool_t0 = 0.0

    def __enter__(self) -> "TurnView":
        self.spinner.start()
        return self

    def __exit__(self, *exc) -> None:
        self.spinner.stop()
        if self._streamed:
            self.spinner.write(theme.ANSI_RESET + "\n")

    # -- callbacks handed to runtime.handle() ------------------------------

    def on_text(self, chunk: str) -> None:
        self.spinner.pause()
        if not self._streamed:
            self._streamed = True
            marker = theme.SPEAK if self.speaking else theme.ANSI_ACCENT + theme.CARET + theme.ANSI_RESET
            self.spinner.write(
                f"{theme.ANSI_BELLA}Bella{theme.ANSI_RESET} {marker} {theme.ANSI_RESPONSE}"
            )
        self.spinner.write(chunk)

    def on_tool_start(self, name: str, args: dict) -> None:
        self.spinner.pause()
        self._tool_t0 = time.monotonic()
        self.r_tool_start(name, args)
        self.spinner.set_activity(theme.activity_label(name))
        self.spinner.resume()

    def on_tool_end(self, name: str, result_str: str) -> None:
        self.spinner.pause()
        elapsed = time.monotonic() - self._tool_t0
        self.r_tool_end(name, result_str, elapsed)
        self.spinner.thinking()
        self.spinner.resume()

    def on_memory_note(self, target: str, content: str) -> None:
        self.spinner.pause()
        self.r.memory_note(target, content)
        self.spinner.resume()

    # -- final presentation ---------------------------------------------

    def finish(self, result) -> None:
        self.spinner.pause()  # stop the animation before printing anything
        if result.source in ("fast", "local"):
            self.r.bella_reply(result.reply, speaking=self.speaking)
        elif result.source == "empty":
            self.r.error("No response", "the model returned nothing — try again")
        # agent / fast_chat already streamed via on_text / __exit__
        self.r.route_note(result)

    # -- tool line rendering (verbose-aware) ---------------------------

    def r_tool_start(self, name: str, args: dict) -> None:
        c = self.r.console
        title = theme.tool_title(name)
        tag = ""
        try:
            from tools.permissions import evaluate_tool_call

            decision = evaluate_tool_call(name, args)
            if decision.dangerous:
                tag = f" [warning]{theme.WARN} dangerous[/]"
        except Exception:
            pass
        c.print(f"[accent]{theme.TOOL}[/] [accent]{escape(title)}[/]{tag}")

        if name == "shell" and args.get("command"):
            c.print(f"  [muted]{escape(str(args['command']))}[/]")
        if self.r.verbose:
            c.print(f"  [muted]{escape(json.dumps(args, ensure_ascii=False))}[/]")

    def r_tool_end(self, name: str, result_str: str, elapsed: float) -> None:
        c = self.r.console
        try:
            result = json.loads(result_str)
        except Exception:
            result = {"raw": result_str}

        ok, summary, detail = _summarize(name, result)
        timing = ""
        if self.r.verbose or elapsed >= 0.5:
            timing = f"  [muted]{elapsed:.2f}s[/]"

        if ok:
            c.print(f"[success]{theme.OK}[/] [response]{escape(summary)}[/]{timing}")
        else:
            c.print(f"[error]{theme.FAIL} {escape(theme.tool_title(name))}[/]{timing}")
            for line in detail:
                c.print(f"  [muted]{escape(line)}[/]")

        if self.r.verbose:
            for line in _verbose_output(name, result):
                c.print(f"  [muted]{escape(line)}[/]")


# ── per-tool result summaries ──────────────────────────────────────────────

def _summarize(name: str, result: dict) -> tuple[bool, str, list[str]]:
    """Return (ok, one_line_summary, error_detail_lines)."""
    err = result.get("error")

    if name == "shell":
        code = result.get("exit_code", 0)
        if err:
            return False, "", [str(err)]
        if code != 0:
            stderr = (result.get("stderr") or "").strip().splitlines()
            first = stderr[0] if stderr else ""
            return False, "", [f"command failed · exit {code}", first] if first else [f"command failed · exit {code}"]
        return True, "shell", []

    if err:
        detail = [str(err)]
        if result.get("candidates"):
            detail.append("candidates: " + ", ".join(result["candidates"]))
        return False, "", detail
    if result.get("success") is False:
        return False, "", [str(result.get("stderr") or "failed")]

    if name == "web_search":
        return True, f"{result.get('count', len(result.get('results', [])))} results", []
    if name == "recall":
        return True, f"{result.get('count', 0)} matches", []
    if name == "spotify":
        track = (result.get("track") or "").strip(" —")
        action = result.get("action", "")
        state = result.get("player_state", "")
        if track and (state == "playing" or action.startswith("play")):
            return True, f"Playing {track}", []
        if track:
            return True, f"{track} ({state or 'paused'})", []
        if state in ("", "not running", "stopped") or action == "current":
            return True, "nothing playing", []
        return True, action or "done", []
    if name == "volume":
        vol = result.get("volume")
        tag = " (muted)" if result.get("muted") else ""
        return True, f"Volume {vol}{tag}" if vol is not None else "done", []
    if name == "reminder":
        if "reminders" in result:
            return True, f"{result.get('count', 0)} reminders", []
        if result.get("added"):
            due = f" · {result['due_date']}" if result.get("due_date") else ""
            return True, f"Reminder added{due}", []
        return True, "done", []
    if name == "todo":
        if "todos" in result:
            return True, f"{result.get('count', 0)} tasks", []
        return True, "tasks updated", []
    if name == "memory":
        if "chars" in result:
            return True, f"memory updated · {result['chars']}/{result.get('limit', 0)} chars", []
        return True, "memory updated", []
    if name == "github":
        url = result.get("repo_url") or result.get("remote_url")
        if "has_uncommitted_changes" in result:  # status
            state = "changes pending" if result["has_uncommitted_changes"] else "clean"
            return True, state + (f" · {url}" if url else ""), []
        act = result.get("action", "done").replace("_", " ")
        return True, act + (f" · {url}" if url else ""), []
    if name == "read_file":
        p = result.get("path", "")
        return True, f"read {Path(p).name}" if p else "read", []
    if name == "write_file":
        p = result.get("path", "")
        return True, f"wrote {Path(p).name}" if p else "wrote", []
    if name == "open_browser":
        return True, f"opened {result.get('url', '')}", []

    return True, "done", []


def _verbose_output(name: str, result: dict) -> list[str]:
    """Extra raw lines shown only in verbose mode."""
    lines: list[str] = []
    if name == "shell":
        out = (result.get("stdout") or "").strip().splitlines()
        lines += out[:20]
        if len(out) > 20:
            lines.append(f"… ({len(out) - 20} more lines)")
    elif name == "web_search":
        for r in result.get("results", [])[:3]:
            lines.append(r.get("title", ""))
            lines.append("  " + (r.get("snippet", "") or "")[:120])
    elif name == "recall":
        for r in result.get("results", [])[:3]:
            lines.append((r.get("content", "") or "")[:120])
    elif name == "reminder" and "reminders" in result:
        for r in result["reminders"]:
            due = f" — {r['due']}" if r.get("due") else ""
            lines.append(f"{r['name']}{due}")
    return lines
