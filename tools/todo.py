"""tools/todo.py — the user's local todo list at ~/todo/todos.json.

NOT Apple Reminders — that's the separate `reminder` tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.base import PermissionLevel, Tool
from tools.registry import register

TODO_FILE = Path.home() / "todo" / "todos.json"


def _load_todos() -> list:
    if not TODO_FILE.exists():
        return []
    try:
        return json.loads(TODO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_todos(todos: list) -> None:
    TODO_FILE.parent.mkdir(parents=True, exist_ok=True)
    TODO_FILE.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")


def run_todo(action: str, text: str = None, position: int = None) -> dict:
    todos = _load_todos()

    if action == "list":
        return {"todos": todos, "count": len(todos)}

    elif action == "add":
        if not text:
            return {"error": "text is required for add"}
        todos.append({"text": text, "done": False})
        _save_todos(todos)
        return {"success": True, "added": text, "position": len(todos)}

    elif action == "insert":
        if not text or position is None:
            return {"error": "text and position are required for insert"}
        if position < 1 or position > len(todos) + 1:
            return {"error": f"Position {position} out of range (1-{len(todos)+1})"}
        todos.insert(position - 1, {"text": text, "done": False})
        _save_todos(todos)
        return {"success": True, "inserted": text, "at": position}

    elif action == "delete":
        if position is None:
            return {"error": "position is required for delete"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1-{len(todos)})"}
        removed = todos.pop(position - 1)
        _save_todos(todos)
        return {"success": True, "deleted": removed["text"]}

    elif action == "modify":
        if position is None or not text:
            return {"error": "position and text are required for modify"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1-{len(todos)})"}
        old = todos[position - 1]["text"]
        todos[position - 1]["text"] = text
        _save_todos(todos)
        return {"success": True, "old": old, "new": text}

    elif action in ("done", "undone"):
        if position is None:
            return {"error": f"position is required for {action}"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1-{len(todos)})"}
        todos[position - 1]["done"] = (action == "done")
        _save_todos(todos)
        return {"success": True, action: todos[position - 1]["text"]}

    return {"error": f"Unknown todo action: {action}"}


class TodoTool(Tool):
    name = "todo"
    description = (
        "Manage the user's local todo list (NOT Apple Reminders — use the "
        "`reminder` tool for that). Actions: list, add, insert, delete, modify, "
        "done, undone."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "insert", "delete", "modify", "done", "undone"],
                "description": "The todo operation.",
            },
            "text": {"type": "string", "description": "Task text (for add, insert, modify)."},
            "position": {"type": "integer", "description": "1-based position (for insert, delete, modify, done, undone)."},
        },
        "required": ["action"],
    }
    permission = PermissionLevel.LOW_RISK

    def execute(self, action: str, text: str = None, position: int = None) -> dict:
        return run_todo(action, text, position)


register(TodoTool())
