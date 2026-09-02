"""
llm/ollama_client.py — Ollama client with streaming + tool-use agentic loop.
"""

import os
import json
from typing import Callable

import ollama

from tools.executor import TOOL_SCHEMAS, execute_tool

DEFAULT_MODEL = "qwen3:8b"
MODEL = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)

# Keep num_ctx identical everywhere we talk to Ollama. Changing num_ctx between
# calls forces Ollama to drop and re-allocate the model's KV cache, which adds
# several seconds of "reload" latency to the next turn.
NUM_CTX = int(os.getenv("CTX_SIZE", "4096"))
NUM_PREDICT = int(os.getenv("MAX_TOKENS", "2048"))

# Hard cap on tool-call round trips per turn so a confused model can't loop forever.
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "12"))

BASE_OPTIONS = {
    "num_predict": NUM_PREDICT,
    "num_ctx": NUM_CTX,
}

# qwen3 etc. emit a long <think> preamble by default; disabling it is a big
# latency win. Passed as a top-level kwarg (not an option) in ollama >= 0.5.
NO_THINK = os.getenv("OLLAMA_THINK", "").lower() not in ("1", "true", "yes")


def _to_ollama_tools(schemas: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in schemas
    ]


OLLAMA_TOOLS = _to_ollama_tools(TOOL_SCHEMAS)


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


def _chat_stream(messages: list[dict]):
    """ollama.chat, retrying without `think` for models that reject the param."""
    try:
        return ollama.chat(
            model=MODEL, messages=messages, tools=OLLAMA_TOOLS,
            stream=True, think=not NO_THINK, options=BASE_OPTIONS,
        )
    except (ollama.ResponseError, TypeError):
        return ollama.chat(
            model=MODEL, messages=messages, tools=OLLAMA_TOOLS,
            stream=True, options=BASE_OPTIONS,
        )


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def run_agent_turn(
    messages: list[dict],
    system: str,
    on_text: Callable[[str], None],
    on_tool_start: Callable[[str, dict], None],
    on_tool_end: Callable[[str, str], None],
) -> tuple[str, list[dict]]:
    """
    Run one full agentic turn until the model returns with no tool calls.
    Returns (final_text, updated_messages).

    `final_text` and the stored assistant messages contain only user-visible
    text — <think> spans are stripped so they don't pollute history or context.
    """
    messages = list(messages)
    think = _ThinkFilter()
    final_text_parts: list[str] = []

    for _iteration in range(MAX_TOOL_ITERATIONS):
        clean_parts: list[str] = []
        tool_calls: list = []

        try:
            stream = _chat_stream(
                [{"role": "system", "content": system}] + messages
            )

            for chunk in stream:
                msg = chunk.get("message", {})

                content = msg.get("content", "")
                if content:
                    clean = think.feed(content)
                    if clean:
                        on_text(clean)
                        clean_parts.append(clean)

                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])

        except Exception as e:
            on_text(f"\n[Ollama error: {e}]\n")
            break

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
            result_str = execute_tool(tool_name, tool_input)
            on_tool_end(tool_name, result_str)

            messages.append(
                {"role": "tool", "tool_name": tool_name, "content": result_str}
            )
    else:
        on_text(f"\n[Stopped after {MAX_TOOL_ITERATIONS} tool iterations]\n")

    return "".join(final_text_parts), messages
