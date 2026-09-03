"""
tools/base.py — the common interface every Bella tool implements, plus the
permission-level vocabulary used to reason about a tool call before it runs.

A tool is a small object exposing:
  - name           : stable identifier the LLM and prompts use (do NOT rename
                     casually — existing prompts/behaviour depend on these)
  - description     : text handed to the model
  - input_schema    : JSON Schema for the arguments (Claude "input_schema" shape;
                      the Ollama adapter reshapes it)
  - permission      : the *baseline* PermissionLevel for the tool
  - available / unavailable_reason : whether the tool can run on this platform
  - execute(**kwargs) -> dict : do the work, return a JSON-serialisable dict

`assess(arguments)` returns the *effective* permission level for one concrete
call — most tools just return their baseline, but e.g. `shell` escalates to
DANGEROUS when it spots `rm -rf` / `sudo` / disk operations, and `memory`
drops to READ_ONLY for `action="read"`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class PermissionLevel(enum.IntEnum):
    """How risky a tool call is. Ordered — higher is riskier."""

    READ_ONLY = 0   # observes state only (web search, read file, recall, memory read)
    LOW_RISK = 1    # a visible, easily-reversed side effect (browser, volume, play/pause, add reminder)
    WRITE = 2        # persists or publishes (write file, git commit/push, package install)
    DANGEROUS = 3    # destructive / privileged (rm, sudo, disk ops, credential/security ops)

    @property
    def label(self) -> str:
        return self.name


class CapabilityError(RuntimeError):
    """Raised (or returned as an error dict) when a tool is asked to run on a
    platform or in an environment where it isn't available."""


#: Where a tool runs in the hub-and-client model.
#:   "server"  — on the Bella brain (shared resources: memory, history, web, the project repo)
#:   "device"  — on whichever device the user is currently using (shell, files, browser, audio)
SERVER = "server"
DEVICE = "device"


class Tool:
    """Base class for a tool. Subclass and set the class attributes, or build
    one imperatively via `SimpleTool`."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    permission: PermissionLevel = PermissionLevel.LOW_RISK

    #: "server" (runs on the brain) or "device" (runs on the target device).
    #: Default server — a new tool must opt in to device execution.
    execution_scope: str = SERVER

    # Overridden by platform-specific tools that can't run here.
    available: bool = True
    unavailable_reason: str | None = None

    def assess(self, arguments: dict[str, Any]) -> PermissionLevel:
        """Effective permission level for this specific call. Override to make
        it argument-dependent."""
        return self.permission

    def execute(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- schema helpers -------------------------------------------------------

    def claude_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class PermissionDecision:
    """The result of evaluating a tool call against the current config."""

    tool: str
    level: PermissionLevel
    allowed: bool
    requires_confirmation: bool
    dangerous: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "permission": self.level.label,
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "dangerous": self.dangerous,
            "reason": self.reason,
        }


@dataclass
class SimpleTool(Tool):
    """Build a tool from a plain callable without writing a subclass."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    permission: PermissionLevel = PermissionLevel.LOW_RISK
    execution_scope: str = SERVER
    handler: Any = None
    available: bool = True
    unavailable_reason: str | None = None
    assessor: Any = None  # optional callable(arguments) -> PermissionLevel

    def assess(self, arguments: dict[str, Any]) -> PermissionLevel:
        if self.assessor is not None:
            try:
                return self.assessor(arguments)
            except Exception:
                return self.permission
        return self.permission

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        if not self.available:
            return {"error": self.unavailable_reason or f"{self.name} is unavailable on this platform"}
        return self.handler(**kwargs)
