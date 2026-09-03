"""
bella.bootstrap — minimal, mostly-lazy startup.

`bootstrap()` initialises only what an interface needs to come up cleanly:
  - config (already a cached singleton)
  - persistent memory: ensure the home dir + files exist and are writable
  - the tool registry: import tool modules, count what's available

It deliberately does NOT touch the microphone, launch Spotify, open an Ollama
connection, or do anything else slow or platform-fragile. Optional integrations
initialise lazily on first use. Deeper checks live in `bella doctor`.

Returns a `BootstrapReport` the interfaces render as a short status header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bella.config import BellaConfig, get_config


@dataclass
class BootstrapReport:
    model: str
    platform: str
    memory_status: str          # "ready" | "unavailable: <reason>"
    memory_ok: bool
    tool_count: int             # available tools
    tools_total: int
    tools_unavailable: list[tuple[str, str]] = field(default_factory=list)
    voice_output_available: bool = False
    voice_input_available: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        """Plain one-liner for logs / `bella serve` (no styling). The CLI's
        startup screen is rendered by bella.interfaces.renderer, not here."""
        mem = "memory ready" if self.memory_ok else f"memory {self.memory_status}"
        return f"{self.model} · {self.tool_count} tools · {mem}"


def _check_memory(config: BellaConfig) -> tuple[bool, str]:
    """Ensure the memory home + files exist and the directory is writable."""
    try:
        from memory.core import MEMORY_FILE, USER_FILE, _ensure_home

        _ensure_home()
        target_dir: Path = MEMORY_FILE.parent
        probe = target_dir / ".bella_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        if not MEMORY_FILE.exists() or not USER_FILE.exists():
            return False, "unavailable: memory files missing after init"
        return True, "ready"
    except Exception as e:
        return False, f"unavailable: {e}"


def bootstrap(config: BellaConfig | None = None) -> BootstrapReport:
    config = config or get_config()
    warnings: list[str] = []
    errors: list[str] = []

    # -- memory --
    memory_ok, memory_status = _check_memory(config)
    if not memory_ok:
        errors.append(f"memory {memory_status}")

    # -- tools --
    try:
        from tools.registry import get_registry

        registry = get_registry()
        tools_total = len(registry.all())
        available = registry.available()
        unavailable = registry.unavailable()
    except Exception as e:  # a broken tool module shouldn't kill startup
        errors.append(f"tool registry failed to load: {e}")
        tools_total = 0
        available = []
        unavailable = []

    for name, reason in unavailable:
        warnings.append(f"{name} unavailable — {reason}")

    # -- voice (capability probe only; nothing is opened) --
    voice_out = config.voice.output_available
    voice_in = config.voice.input_available
    if config.voice.output_enabled and not voice_out:
        warnings.append("spoken replies enabled but ELEVENLABS_API_KEY is not set")

    return BootstrapReport(
        model=config.ollama_model,
        platform=config.platform,
        memory_status=memory_status,
        memory_ok=memory_ok,
        tool_count=len(available),
        tools_total=tools_total,
        tools_unavailable=unavailable,
        voice_output_available=voice_out,
        voice_input_available=voice_in,
        warnings=warnings,
        errors=errors,
    )
