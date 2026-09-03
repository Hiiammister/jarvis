"""tools/shell.py — run a shell command on the user's machine."""

from __future__ import annotations

import subprocess

from tools.base import PermissionLevel, Tool
from tools.permissions import shell_command_level
from tools.registry import register


def run_shell(command: str, timeout: int = 30) -> dict:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


class ShellTool(Tool):
    name = "shell"
    execution_scope = "device"
    description = (
        "Run a shell command on the user's machine. Use for file system ops, "
        "running code, git, etc. Always prefer non-destructive commands. Ask "
        "before deleting files or running risky operations."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)."},
        },
        "required": ["command"],
    }
    permission = PermissionLevel.WRITE

    def assess(self, arguments: dict) -> PermissionLevel:
        return shell_command_level(arguments.get("command", ""))

    def execute(self, command: str, timeout: int = 30) -> dict:
        return run_shell(command, timeout)


register(ShellTool())
