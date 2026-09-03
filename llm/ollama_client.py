"""
llm/ollama_client.py — Ollama client: the full tool-calling agent loop plus a
lightweight no-tools chat path.

  run_agent_turn(...)   FULL_AGENT — streaming + tool-call loop, scoped tool set
  run_fast_chat(...)    FAST_CHAT  — one streamed call, NO tools, small output cap

Both share one `ollama.chat` entry point (`_chat`) so connection / retry /
keep-alive / think-filter behaviour is defined once. Model / context / profile
settings come from bella.config; num_ctx is held constant across every call so
Ollama never has to reallocate the KV cache.

Module constants MODEL / BASE_OPTIONS / NUM_CTX / NO_THINK are kept for
backwards compatibility.
"""

from __future__ import annotations

import json
from typing import Callable

import ollama

from bella.config import get_config
from bella.latency import NULL_TIMINGS, Timings
from tools.registry import get_registry

_cfg = get_config()

DEFAULT_MODEL = "qwen3:8b"
MODEL = _cfg.ollama_model
NUM_CTX = _cfg.ollama_ctx_size
NUM_PREDICT = _cfg.max_tokens
MAX_TOOL_ITERATIONS = _cfg.max_tool_iterations
BASE_OPTIONS = _cfg.base_options
# qwen3 etc. emit a long <think> preamble by default; disabling it is a big
# latency win. Passed as a top-level kwarg (not an option) in ollama >= 0.5.
NO_THINK = not _cfg.think
KEEP_ALIVE = _cfg.keep_alive


def _profile_options(profile: str) -> dict:
    """num_predict for the given generation profile. num_ctx is unchanged."""
    return {"num_ctx": _cfg.ollama_ctx_size, "num_predict": _cfg.profiles.tokens(profile)}


def _ollama_tools(names: list[str] | None = None) -> list[dict]:
    return get_registry().ollama_schemas(names)


class _ThinkFilter:
    """Strip <think>...</think> spans from a stream of chunks (stateful)."""

    def __init__(self):
        self.in_think = False

    def feed(self, raw: str) -> str:
        out = ""
        i = 0
        while i < len(raw):
            if not self.in_think:
                start = raw.find("<think>", i)
                if start == -1:
                    out += raw[i:]
                    break
                out += raw[i:start]
                self.in_think = True
                i = start + len("<think>")
            else:
                end = raw.find("</think>", i)
                if end == -1:
                    break  # rest of chunk is think content — drop it
                self.in_think = False
                i = end + len("</think>")
                if i < len(raw) and raw[i] == "\n":
                    i += 1
        return out


def _chat(messages: list[dict], *, tools: list[dict] | None, options: dict, stream: bool = True):
    """Single Ollama entry point. Retries without `think` for models that
    reject the param. `keep_alive` keeps the model resident between requests."""
    kwargs = dict(model=MODEL, messages=messages, stream=stream,
                  options=options, keep_alive=KEEP_ALIVE)
    if tools:
        kwargs["tools"] = tools
    try:
        return ollama.chat(think=not NO_THINK, **kwargs)
    except (ollama.ResponseError, TypeError):
        return ollama.chat(**kwargs)


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def warmup() -> None:
    """Fire a 1-token generation on startup so (a) the model is resident before
    the first real request and (b) the FAST_CHAT system-prompt prefix is already
    in Ollama's KV cache. Best-effort; never raises."""
    try:
        from bella.prompt import build_system_prompt

        system = build_system_prompt(minimal=True)
    except Exception:
        system = "You are Bella."
    try:
        _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": "hi"}],
            tools=None,
            options={"num_ctx": _cfg.ollama_ctx_size, "num_predict": 1},
            stream=False,
        )
    except Exception:
        pass


# ── FAST_CHAT ──────────────────────────────────────────────────────────────

def run_fast_chat(
    messages: list[dict],
    system: str,
    on_text: Callable[[str], None],
    *,
    profile: str = "FAST",
    timings: Timings | None = None,
) -> str:
    """One streamed model call, no tools, no loop. For conversational / language
    requests the router decided need no tools. Returns the clean reply text."""
    t = timings or NULL_TIMINGS
    think = _ThinkFilter()
    parts: list[str] = []
    first_seen = False

    try:
        t.start("generation")
        stream = _chat(
            [{"role": "system", "content": system}] + list(messages),
            tools=None, options=_profile_options(profile),
        )
        for chunk in stream:
            piece = chunk.get("message", {}).get("content", "")
            if not piece:
                continue
            if not first_seen:
                first_seen = True
                t.mark("ollama first token")
            clean = think.feed(piece)
            if clean:
                on_text(clean)
                parts.append(clean)
    except Exception as e:
        on_text(f"\n[Ollama error: {e}]\n")
    finally:
        t.stop("generation")

    return "".join(parts).strip()


# ── FULL_AGENT ─────────────────────────────────────────────────────────────

def run_agent_turn(
    messages: list[dict],
    system: str,
    on_text: Callable[[str], None],
    on_tool_start: Callable[[str, dict], None],
    on_tool_end: Callable[[str, str], None],
    *,
    tools: list[str] | None = None,
    profile: str = "DEEP",
    timings: Timings | None = None,
    execute_tool: Callable[[str, dict], str] | None = None,
) -> tuple[str, list[dict]]:
    """
    Run one full agentic turn until the model returns with no tool calls.
    Returns (final_text, updated_messages).

    `tools` is a list of tool *names* to expose (None = all). `execute_tool`
    runs a tool call and returns a JSON string — defaults to the local registry,
    but the server passes a device-aware gateway so device-local tools run on
    the right client. `final_text` and the stored assistant messages contain
    only user-visible text — <think> spans are stripped.
    """
    t = timings or NULL_TIMINGS
    messages = list(messages)
    registry = get_registry()
    run_tool = execute_tool or registry.execute
    tool_schemas = _ollama_tools(tools)
    options = _profile_options(profile)
    think = _ThinkFilter()
    final_text_parts: list[str] = []
    first_seen = False

    for _iteration in range(MAX_TOOL_ITERATIONS):
        clean_parts: list[str] = []
        tool_calls: list = []

        try:
            t.start("generation")
            stream = _chat(
                [{"role": "system", "content": system}] + messages,
                tools=tool_schemas, options=options,
            )
            for chunk in stream:
                msg = chunk.get("message", {})
                content = msg.get("content", "")
                if content:
                    if not first_seen:
                        first_seen = True
                        t.mark("ollama first token")
                    clean = think.feed(content)
                    if clean:
                        on_text(clean)
                        clean_parts.append(clean)
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
        except Exception as e:
            on_text(f"\n[Ollama error: {e}]\n")
            t.stop("generation")
            break
        t.stop("generation")

        clean_text = "".join(clean_parts)
        assistant_msg: dict = {"role": "assistant", "content": clean_text}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if clean_text:
            final_text_parts.append(clean_text)

        if not tool_calls:
            break

        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            tool_input = _coerce_args(fn.get("arguments", {}))

            on_tool_start(tool_name, tool_input)
            t.start(f"tool: {tool_name}")
            result_str = run_tool(tool_name, tool_input)
            t.stop(f"tool: {tool_name}")
            on_tool_end(tool_name, result_str)

            messages.append(
                {"role": "tool", "tool_name": tool_name, "content": result_str}
            )
    else:
        on_text(f"\n[Stopped after {MAX_TOOL_ITERATIONS} tool iterations]\n")

    return "".join(final_text_parts), messages
