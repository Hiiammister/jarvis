"""
tools/registry.py — the modular tool registry.

The LLM layer only ever touches this module: `get_registry().schemas()` /
`.ollama_schemas()` for the tool list, and `.execute(name, arguments)` to run
one. It has no knowledge of individual tools.

Each tool lives in its own module (tools/shell.py, tools/spotify.py, …) and
calls `register(...)` at import time. `get_registry()` imports them all once,
lazily, on first use.

`execute()` runs the permission hook (tools.permissions.evaluate_tool_call)
before dispatching — with the default config that always allows, so existing
behaviour is preserved, but a stricter config can make WRITE/DANGEROUS calls
return a "requires confirmation" error instead of running.
"""

from __future__ import annotations

import json
from typing import Any

from tools.base import Tool

# Import order here is also the order tools appear in the schema list handed to
# the model. Kept close to the old TOOL_SCHEMAS order so prompt behaviour with
# the 8B model doesn't shift.
_TOOL_MODULES = [
    "tools.shell",
    "tools.web_search",
    "tools.filesystem",
    "tools.browser",
    "tools.volume",
    "tools.spotify",
    "tools.reminders",
    "tools.todo",
    "tools.memory_tool",
    "tools.github",
    "tools.recall",
]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration -------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"tool {tool!r} has no name")
        # Last registration wins, but warn loudly-ish via the return path.
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def available(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.available]

    def unavailable(self) -> list[tuple[str, str]]:
        return [
            (t.name, t.unavailable_reason or "unavailable")
            for t in self._tools.values() if not t.available
        ]

    # -- schemas ----------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        """Claude-style schema list (name/description/input_schema).

        Unavailable tools are still listed so the model can explain the
        capability gap instead of hallucinating around a missing tool; their
        execute() returns a clear capability error.
        """
        return [t.claude_schema() for t in self._tools.values()]

    def ollama_schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Ollama-format schema list. `names` restricts the set (used by the
        router to hand the agent only likely-relevant tools); unknown names are
        ignored, and an empty result falls back to every tool so the model is
        never left with nothing."""
        if names:
            wanted = [self._tools[n] for n in names if n in self._tools]
            if wanted:
                return [t.ollama_schema() for t in wanted]
        return [t.ollama_schema() for t in self._tools.values()]

    # -- execution ------------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        """Run a tool by name. Returns a JSON string (same contract as the old
        tools.executor.execute_tool)."""
        arguments = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Permission hook — see tools/permissions.py.
        try:
            from tools.permissions import evaluate_tool_call
            decision = evaluate_tool_call(name, arguments)
        except Exception:
            decision = None

        if decision is not None and not decision.allowed:
            return json.dumps({
                "error": (
                    f"Tool call blocked: '{name}' is classified {decision.level.label} "
                    f"and the current config requires confirmation for it."
                ),
                "permission": decision.as_dict(),
            })

        if not tool.available:
            return json.dumps({
                "error": tool.unavailable_reason or f"{name} is not available on this platform",
                "unavailable": True,
            })

        try:
            result = tool.execute(**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError as e:
            return json.dumps({"error": f"bad arguments for {name}: {e}"})
        except Exception as e:
            return json.dumps({"error": str(e)})


_REGISTRY: ToolRegistry | None = None
_LOADED = False


def register(tool: Tool) -> Tool:
    """Called by tool modules at import time."""
    get_registry(_loading=True).register(tool)
    return tool


def get_registry(_loading: bool = False) -> ToolRegistry:
    global _REGISTRY, _LOADED
    if _REGISTRY is None:
        _REGISTRY = ToolRegistry()
    if not _LOADED and not _loading:
        _LOADED = True
        import importlib
        for mod in _TOOL_MODULES:
            importlib.import_module(mod)
    return _REGISTRY
