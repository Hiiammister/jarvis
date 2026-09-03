"""tools/memory_tool.py — the `memory` and `recall`-adjacent persistent stores.

Thin wrapper over memory.core; that module stays the single source of truth for
the MEMORY.md / USER.md file format and the Hermes-shared location.
"""

from __future__ import annotations

from memory.core import memory_add, memory_read, memory_remove, memory_replace

from tools.base import PermissionLevel, Tool
from tools.registry import register


def run_memory(action: str, target: str, content: str = None, old_text: str = None) -> dict:
    if action == "add":
        if not content:
            return {"error": "content is required for add"}
        return memory_add(target, content)
    elif action == "replace":
        if not old_text or not content:
            return {"error": "old_text and content are required for replace"}
        return memory_replace(target, old_text, content)
    elif action == "remove":
        if not old_text:
            return {"error": "old_text is required for remove"}
        return memory_remove(target, old_text)
    elif action == "read":
        return memory_read(target)
    return {"error": f"Unknown memory action: {action}"}


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Manage your persistent memory. Two stores:\n"
        "  - 'memory': your personal notes (environment facts, learned conventions, completed work)\n"
        "  - 'user': user profile (preferences, communication style, name/timezone)\n"
        "Actions: add, replace, remove, read.\n"
        "Save proactively when you learn something worth remembering across sessions."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "read"],
                "description": "Memory operation.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which store to modify.",
            },
            "content": {"type": "string", "description": "New entry content (for add and replace)."},
            "old_text": {"type": "string", "description": "Unique substring identifying the entry to replace or remove."},
        },
        "required": ["action", "target"],
    }
    permission = PermissionLevel.WRITE

    def assess(self, arguments: dict) -> PermissionLevel:
        return PermissionLevel.READ_ONLY if arguments.get("action") == "read" else PermissionLevel.WRITE

    def execute(self, action: str, target: str, content: str = None, old_text: str = None) -> dict:
        return run_memory(action, target, content, old_text)


register(MemoryTool())
