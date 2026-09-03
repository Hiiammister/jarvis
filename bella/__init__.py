"""
bella — the personal AI workstation, refactored into a bootstrap/runtime core
with thin CLI, voice, and HTTP interfaces on top.

Layers:
  bella.config      centralized environment/config loading
  bella.bootstrap   minimal, lazy startup -> BootstrapReport
  bella.runtime     BellaRuntime: conversation state + agent turn (shared by every interface)
  bella.prompt      system-prompt construction (SOUL.md + memory + time + tool rules)
  bella.router      fast-path intent detection (media/volume/reminders) before the LLM
  bella.doctor      `bella doctor` diagnostics
  bella.interfaces  cli / voice front ends
  bella.server      FastAPI remote interface
"""

__all__ = ["__version__"]

__version__ = "0.2.0"
