"""tools/filesystem.py — read_file / write_file."""

from __future__ import annotations

from pathlib import Path

from tools.base import PermissionLevel, Tool
from tools.registry import register


def run_read_file(path: str) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"error": f"File not found: {p}"}
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "path": str(p), "size": len(content)}
    except Exception as e:
        return {"error": str(e)}


def run_write_file(path: str, content: str, append: bool = False) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": str(p), "bytes": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


class ReadFileTool(Tool):
    name = "read_file"
    execution_scope = "device"
    description = "Read the contents of a local file."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~-relative file path."},
        },
        "required": ["path"],
    }
    permission = PermissionLevel.READ_ONLY

    def execute(self, path: str) -> dict:
        return run_read_file(path)


class WriteFileTool(Tool):
    name = "write_file"
    execution_scope = "device"
    description = "Write or create a local file. Creates parent directories if missing."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~-relative file path."},
            "content": {"type": "string", "description": "File content to write."},
            "append": {"type": "boolean", "description": "Append instead of overwrite (default false)."},
        },
        "required": ["path", "content"],
    }
    permission = PermissionLevel.WRITE

    def execute(self, path: str, content: str, append: bool = False) -> dict:
        return run_write_file(path, content, append)


register(ReadFileTool())
register(WriteFileTool())
