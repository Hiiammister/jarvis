"""
bella.runtime — the shared brain.

Every interface (CLI, voice, FastAPI/WebSocket) drives the ONE BellaRuntime:

        user input  (+ which device it came from)
            |
        resolve target device   (tool_gateway, if server-hosted)
            |
        classify()  (bella.router — pure, no model)
            |
     +------+---------+-----------+--------------+
     |      |         |           |              |
   LOCAL  SLASH   DIRECT_TOOL  FAST_CHAT     FULL_AGENT
  (canned)(iface)  (1 tool)  (no-tools LLM)  (scoped tools + agent loop)

Device-local tool calls are dispatched through an injected `tool_gateway`
(server-side) so they run on the *source* device (or an explicitly named one);
with no gateway (the CLI) every tool runs in-process exactly as before.

It owns no UI and no transport: interfaces pass callbacks for streamed text /
tool events and read `TurnResult`.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from memory.core import Session

from bella.config import BellaConfig, get_config
from bella.latency import NULL_TIMINGS, Timings
from bella.memory_extract import extract_and_save, should_extract_memory
from bella.router import Route, RouteDecision, RouterMatch, classify, format_reply, tools_for_groups

Noop = lambda *a, **k: None  # noqa: E731


@dataclass
class TurnResult:
    reply: str
    source: str  # "local" | "fast" | "fast_chat" | "agent" | "empty"
    tool: str | None = None
    router_match: RouterMatch | None = None
    route: str = ""
    profile: str = ""
    target_device: str | None = None
    timings: Timings | None = None

    @property
    def spoke_via_stream(self) -> bool:
        return self.source in ("agent", "fast_chat")


@dataclass
class _Turn:
    """Per-turn device context. `execute` is what the tool layers call."""

    text: str                       # possibly device-clause-stripped
    original: str
    source_device: str | None
    target_device: str | None
    target_name: str
    explicit_target: bool
    gateway: object | None

    def execute(self, name: str, arguments: dict) -> str:
        if self.gateway is not None:
            return self.gateway.execute(
                name, arguments,
                source_device_id=self.source_device,
                target_device_id=self.target_device,
            )
        from tools.registry import get_registry

        return get_registry().execute(name, arguments)


@dataclass
class BellaRuntime:
    config: BellaConfig = field(default_factory=get_config)
    session: Session | None = None
    start_session: bool = True
    #: server-side hub only — routes device tools to the right client
    tool_gateway: object | None = None

    def __post_init__(self) -> None:
        self.messages: list[dict] = []
        self._bg_threads: list[threading.Thread] = []
        self._system_cache: dict[tuple, str] = {}
        if self.session is None and self.start_session:
            self.session = Session()
        if self.start_session and self.config.warmup:
            threading.Thread(target=_warmup_safe, daemon=True).start()

    # -- system prompt (cached per shape) -------------------------------

    def system_prompt(self, *, voice_mode: bool = False,
                      tool_names: list[str] | None = None,
                      minimal: bool = False) -> str:
        key = (voice_mode, minimal, tuple(tool_names) if tool_names else None)
        if key not in self._system_cache:
            from bella.prompt import build_system_prompt

            self._system_cache[key] = build_system_prompt(
                voice_mode=voice_mode, tool_names=tool_names, minimal=minimal
            )
        return self._system_cache[key]

    def reset_conversation(self) -> None:
        self.messages = []
        self._system_cache.clear()

    # -- background jobs ---------------------------------------------

    def spawn_bg(self, target: Callable, *args) -> threading.Thread:
        self._bg_threads[:] = [t for t in self._bg_threads if t.is_alive()]
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._bg_threads.append(t)
        return t

    def join_bg(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        for t in self._bg_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(remaining)

    def shutdown(self) -> None:
        self.join_bg()
        if self.session is not None:
            self.session.close()

    # -- message trimming ---------------------------------------------

    @staticmethod
    def trim_messages(messages: list[dict], max_turns: int = 10) -> list[dict]:
        cutoff = max_turns * 2
        return messages if len(messages) <= cutoff else messages[-cutoff:]

    def _last_was_question(self) -> bool:
        for m in reversed(self.messages):
            if m["role"] == "assistant" and isinstance(m["content"], str):
                return m["content"].rstrip().endswith("?")
            if m["role"] == "user":
                return False
        return False

    def _add_user(self, text: str, device: str | None) -> None:
        if self.session is not None:
            self.session.add_message("user", text, device=device)

    def _add_assistant(self, text: str) -> None:
        if self.session is not None:
            self.session.add_message("assistant", text)

    # -- the turn -----------------------------------------------------

    def handle(
        self,
        user_input: str,
        *,
        voice_mode: bool = False,
        source_device: str | None = None,
        on_text: Callable[[str], None] = Noop,
        on_tool_start: Callable[[str, dict], None] = Noop,
        on_tool_end: Callable[[str, str], None] = Noop,
        on_memory_note: Callable[[str, str], None] | None = None,
        timings: Timings | None = None,
    ) -> TurnResult:
        t = timings or NULL_TIMINGS

        # 1. Which device do this turn's device-local actions run on?
        text = user_input
        target_device, target_name, explicit = source_device, "", False
        if self.tool_gateway is not None:
            with t.measure("target"):
                res = self.tool_gateway.resolve_target(user_input, source_device)
            text = res.cleaned_text
            target_device, target_name, explicit = res.device_id, res.device_name, res.explicit

        turn = _Turn(text=text, original=user_input, source_device=source_device,
                     target_device=target_device, target_name=target_name,
                     explicit_target=explicit, gateway=self.tool_gateway)

        # 2. A named-but-unknown target — don't guess, tell the user.
        if explicit and target_device is None:
            known = self._known_devices_str()
            reply = f"I don't know a device called “{target_name}”. {known}"
            self._add_user(user_input, source_device)
            self._add_assistant(reply)
            self.messages += [{"role": "user", "content": user_input},
                              {"role": "assistant", "content": reply}]
            return TurnResult(reply, "local", route="DEVICE_UNKNOWN", timings=t)

        with t.measure("route"):
            decision = classify(text, last_was_question=self._last_was_question())

        if decision.route is Route.LOCAL_RESPONSE:
            return self._handle_local(turn, decision, t)
        if decision.route is Route.SLASH_COMMAND:
            reply = decision.response or "Slash commands aren't available on this interface."
            return TurnResult(reply, "local", route=decision.route.value, timings=t)
        if decision.route is Route.DIRECT_TOOL:
            return self._handle_direct_tool(turn, decision, on_tool_start, on_tool_end, t)
        if decision.route is Route.FAST_CHAT:
            return self._handle_fast_chat(turn, decision, on_text, on_memory_note, t)
        return self._handle_agent(turn, decision, voice_mode, on_text,
                                  on_tool_start, on_tool_end, on_memory_note, t)

    def _known_devices_str(self) -> str:
        if self.tool_gateway is None:
            return ""
        try:
            names = [d.device_name for d in self.tool_gateway.registry.list(connected_only=True)]
            return "Connected: " + ", ".join(names) + "." if names else "No devices are connected."
        except Exception:
            return ""

    # -- LOCAL_RESPONSE ---------------------------------------------

    def _handle_local(self, turn: _Turn, decision: RouteDecision, t: Timings) -> TurnResult:
        reply = decision.response or ""
        self._add_user(turn.original, turn.source_device)
        self._add_assistant(reply)
        self.messages += [{"role": "user", "content": turn.original},
                          {"role": "assistant", "content": reply}]
        return TurnResult(reply, "local", route=decision.route.value,
                          profile=decision.profile, target_device=turn.target_device, timings=t)

    # -- DIRECT_TOOL -----------------------------------------------

    def _handle_direct_tool(self, turn: _Turn, decision: RouteDecision,
                            on_tool_start, on_tool_end, t: Timings) -> TurnResult:
        self._add_user(turn.original, turn.source_device)

        tool, args = decision.tool_name, decision.tool_args or {}
        on_tool_start(tool, args)
        with t.measure(f"tool: {tool}"):
            result_str = turn.execute(tool, args)
        on_tool_end(tool, result_str)

        try:
            result = json.loads(result_str)
        except Exception:
            result = {"raw": result_str}

        reply = format_reply(tool, args, result)
        self._add_assistant(reply)
        self.messages += [{"role": "user", "content": turn.text},
                          {"role": "assistant", "content": reply}]
        return TurnResult(reply, "fast", tool=tool, router_match=RouterMatch(tool, args),
                          route=decision.route.value, target_device=turn.target_device, timings=t)

    # -- FAST_CHAT -----------------------------------------------

    def _handle_fast_chat(self, turn: _Turn, decision: RouteDecision,
                          on_text, on_memory_note, t: Timings) -> TurnResult:
        from llm.ollama_client import run_fast_chat

        self._add_user(turn.original, turn.source_device)
        working = self.messages + [{"role": "user", "content": turn.text}]

        with t.measure("prompt"):
            system = self.system_prompt(minimal=True)
        history = self.trim_messages(working, max_turns=3)

        reply = run_fast_chat(history, system, on_text, profile=decision.profile, timings=t)
        if not reply:
            return TurnResult("", "empty", route=decision.route.value, timings=t)

        self._add_assistant(reply)
        if should_extract_memory(turn.text, reply):
            self.spawn_bg(extract_and_save, turn.text, reply, on_memory_note)
        self.messages = working + [{"role": "assistant", "content": reply}]
        return TurnResult(reply, "fast_chat", route=decision.route.value,
                          profile=decision.profile, target_device=turn.target_device, timings=t)

    # -- FULL_AGENT --------------------------------------------

    def _handle_agent(self, turn: _Turn, decision: RouteDecision, voice_mode: bool,
                      on_text, on_tool_start, on_tool_end, on_memory_note, t: Timings) -> TurnResult:
        from llm.ollama_client import run_agent_turn

        tool_names = tools_for_groups(decision.tool_groups) or None

        self._add_user(turn.original, turn.source_device)
        working = self.messages + [{"role": "user", "content": turn.text}]

        with t.measure("prompt"):
            system = self.system_prompt(voice_mode=voice_mode, tool_names=tool_names)

        reply_text, _ = run_agent_turn(
            messages=self.trim_messages(working),
            system=system,
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            tools=tool_names,
            profile=decision.profile,
            timings=t,
            execute_tool=turn.execute,
        )

        if not reply_text:
            return TurnResult("", "empty", route=decision.route.value, timings=t)

        self._add_assistant(reply_text)
        if should_extract_memory(turn.text, reply_text):
            self.spawn_bg(extract_and_save, turn.text, reply_text, on_memory_note)
        self.messages = working + [{"role": "assistant", "content": reply_text}]
        return TurnResult(reply_text, "agent", route=decision.route.value,
                          profile=decision.profile, target_device=turn.target_device, timings=t)


def _warmup_safe() -> None:
    try:
        from llm.ollama_client import warmup

        warmup()
    except Exception:
        pass
