#!/usr/bin/env python3
"""
jarvis.py — thin entry point (kept for compatibility).

The implementation moved into the `bella` package:

    bella.config        environment / configuration
    bella.bootstrap     minimal startup -> BootstrapReport
    bella.runtime       BellaRuntime — the shared brain (CLI + server both use it)
    bella.prompt        system-prompt construction
    bella.router        fast-path intent detection (media / volume / reminders)
    bella.interfaces.cli / .voice
    bella.server        FastAPI remote interface
    tools.registry      modular tool registry (was tools/executor.py)

Usage (unchanged):
    python jarvis.py                     # interactive REPL
    python jarvis.py "do something"      # single-shot mode

New, equivalent:
    python -m bella                      # or:  bin/bella
    bin/bella "do something"
    bin/bella doctor | serve | voice
"""

import sys

from bella.cli_entry import main

# ── Backwards-compatible re-exports ──────────────────────────────────────────
# A few names lived in this module historically. Keep them importable so any
# personal scripts that did `from jarvis import ...` don't break.
from bella.prompt import build_system_prompt  # noqa: F401,E402
from bella.router import (  # noqa: F401,E402
    _fast_media_call,
    _fast_media_reply,
    _fast_reminder_call,
)
from bella.runtime import BellaRuntime  # noqa: F401,E402
from bella.memory_extract import extract_and_save as _extract_and_save_memory  # noqa: F401,E402

_trim_messages = BellaRuntime.trim_messages


def run_repl(single_shot: str | None = None) -> None:
    """Compatibility wrapper for the old entry function."""
    if single_shot:
        from bella.cli_entry import _single_shot
        _single_shot(single_shot)
    else:
        # Preserve the historical `python jarvis.py` default: interactive
        # listen mode (say "Bella" or press Enter). `bella` / `bella chat`
        # start text-first instead; `bella voice` matches this.
        from bella.cli_entry import _chat
        _chat(start_mode="listen")


if __name__ == "__main__":
    # `python jarvis.py` with no args keeps its historical default of the
    # interactive listen-mode REPL. Any argument (a subcommand or a quoted
    # single-shot request) goes through the normal `bella` dispatch.
    if len(sys.argv) == 1:
        run_repl()
    else:
        sys.exit(main())
