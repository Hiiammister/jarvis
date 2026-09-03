"""filesystem capability — read_file / write_file on the local disk. Cross-platform."""

from __future__ import annotations

from pathlib import Path

CAPABILITY = "filesystem"

_MAX_READ = 512 * 1024  # don't ship a huge file back over the socket


def handle(tool: str, args: dict) -> dict:
    if tool == "read_file":
        return _read(args.get("path", ""))
    if tool == "write_file":
        return _write(args.get("path", ""), args.get("content", ""), bool(args.get("append")))
    return {"error": f"filesystem: unknown tool {tool}"}


def _read(path: str) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"error": f"file not found: {p}"}
        data = p.read_text(encoding="utf-8", errors="replace")
        truncated = len(data) > _MAX_READ
        return {"content": data[:_MAX_READ], "path": str(p), "size": len(data),
                "truncated": truncated}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _write(path: str, content: str, append: bool) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": str(p), "bytes": len(content.encode())}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
