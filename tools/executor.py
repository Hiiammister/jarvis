"""
tools/executor.py — backwards-compatible shim over the modular tool registry.

The tool implementations moved to individual modules (tools/shell.py,
tools/spotify.py, …) collected by tools/registry.py. This module keeps the old
public surface working so nothing that did `from tools.executor import
TOOL_SCHEMAS, execute_tool` breaks.

New code should use `tools.registry.get_registry()` directly.
"""

from __future__ import annotations

from typing import Any

from tools.registry import get_registry

# Legacy per-tool entry points, re-exported for any callers that used them.
from tools.shell import run_shell  # noqa: F401
from tools.web_search import run_web_search  # noqa: F401
from tools.filesystem import run_read_file, run_write_file  # noqa: F401
from tools.browser import run_open_browser  # noqa: F401
from tools.todo import run_todo  # noqa: F401
from tools.memory_tool import run_memory  # noqa: F401
from tools.github import run_github  # noqa: F401
from tools.recall import run_recall  # noqa: F401


def _schemas() -> list[dict]:
    return get_registry().schemas()


# Computed once at import — same shape as before (list of
# {name, description, input_schema}).
TOOL_SCHEMAS: list[dict[str, Any]] = _schemas()


def execute_tool(name: str, inputs: dict | None) -> str:
    """Execute a tool by name and return a JSON string result."""
    return get_registry().execute(name, inputs or {})
