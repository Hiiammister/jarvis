"""
tools/permissions.py — evaluate a tool call before it runs.

This is the enforcement *hook* asked for in the refactor. By default it is
permissive (matching Bella's current behaviour: everything runs), but it now
gives the runtime and interfaces a single place to decide whether a call needs
confirmation or should be blocked.

Config flags (see bella.config.BellaConfig):
  - allow_unconfirmed_write  (default True)  -> WRITE calls run without a prompt
  - allow_dangerous          (default True)  -> DANGEROUS calls are still allowed

Set either to False to make the corresponding class of call return a
"requires confirmation" / "blocked" error instead of executing.
"""

from __future__ import annotations

import re
from typing import Any

from tools.base import PermissionDecision, PermissionLevel

# Shell command fragments that mean "this can destroy data or change the
# system". Deliberately conservative — it flags for *awareness*, it does not
# block by default.
_DANGEROUS_SHELL_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]",   # rm -rf / rm -fr / rm -r -f
        r"\brm\s+-[a-z]*f",
        r"\brmdir\b",
        r"\bsudo\b",
        r"\bdoas\b",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bdiskutil\s+(erase|partition|reformat)",
        r"\bfdisk\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bhalt\b",
        r">\s*/dev/(sd|disk|nvme)",
        r":\(\)\s*\{",                         # fork bomb
        r"\bchown\s+-R\s+/(?!\w)",
        r"\bchmod\s+-R\s+[0-7]{3,4}\s+/(?!\w)",
        r"\bkillall\b",
        r"\bmv\s+.*\s+/dev/null",
        r"\bgit\s+.*\bpush\b.*--force",
        r"\bgit\s+reset\s+--hard\b",
        r"\b(security|keychain)\b.*\b(dump|delete|find-generic-password)",
        r"\bdefaults\s+delete\b",
    ]
]


def shell_command_level(command: str) -> PermissionLevel:
    """Classify a shell command. READ_ONLY for obvious inspect-only commands,
    DANGEROUS if it matches a destructive pattern, WRITE otherwise."""
    cmd = (command or "").strip()
    for pat in _DANGEROUS_SHELL_PATTERNS:
        if pat.search(cmd):
            return PermissionLevel.DANGEROUS

    first = re.split(r"\s|\|", cmd, 1)[0].rsplit("/", 1)[-1]
    read_only_bins = {
        "ls", "pwd", "cat", "less", "head", "tail", "grep", "rg", "find", "fd",
        "echo", "which", "whoami", "date", "uname", "env", "printenv", "df",
        "du", "ps", "top", "stat", "file", "wc", "tree", "hostname",
    }
    if first in read_only_bins:
        return PermissionLevel.READ_ONLY
    if cmd.startswith("git ") and re.match(r"git\s+(status|log|diff|show|branch|remote|config\s+--get)", cmd):
        return PermissionLevel.READ_ONLY
    return PermissionLevel.WRITE


def evaluate_tool_call(tool_name: str, arguments: dict[str, Any], config: Any = None) -> PermissionDecision:
    """Decide what should happen with `tool_name(**arguments)` given `config`.

    Returns a PermissionDecision. `allowed` reflects the current config; the
    runtime/interfaces may still choose to prompt the user when
    `requires_confirmation` is set.
    """
    from tools.registry import get_registry

    if config is None:
        from bella.config import get_config
        config = get_config()

    registry = get_registry()
    tool = registry.get(tool_name)
    if tool is None:
        return PermissionDecision(
            tool=tool_name, level=PermissionLevel.DANGEROUS, allowed=False,
            requires_confirmation=True, dangerous=False,
            reason=f"unknown tool: {tool_name}",
        )

    level = tool.assess(arguments or {})

    allow_write = getattr(config, "allow_unconfirmed_write", True)
    allow_dangerous = getattr(config, "allow_dangerous", True)

    if level <= PermissionLevel.LOW_RISK:
        return PermissionDecision(tool_name, level, allowed=True,
                                  requires_confirmation=False, dangerous=False,
                                  reason="low-risk — auto")
    if level == PermissionLevel.WRITE:
        return PermissionDecision(
            tool_name, level, allowed=bool(allow_write),
            requires_confirmation=not allow_write, dangerous=False,
            reason="write — auto" if allow_write else "write — confirmation required",
        )
    # DANGEROUS
    return PermissionDecision(
        tool_name, level, allowed=bool(allow_dangerous),
        requires_confirmation=True, dangerous=True,
        reason="dangerous — allowed by config" if allow_dangerous else "dangerous — blocked by config",
    )
