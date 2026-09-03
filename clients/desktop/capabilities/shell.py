"""shell capability — run a shell command locally. Cross-platform."""

from __future__ import annotations

import subprocess

CAPABILITY = "shell"


def handle(tool: str, args: dict) -> dict:
    command = args.get("command", "")
    timeout = int(args.get("timeout", 30) or 30)
    if not command:
        return {"error": "no command"}
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
