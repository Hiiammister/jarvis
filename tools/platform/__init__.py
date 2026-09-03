"""
tools/platform — OS detection and per-OS implementations of the tools that
need native hooks (system volume, Spotify control, Reminders).

Tool modules import the right backend from here. On an unsupported OS the
backend is None and the tool registers as unavailable rather than crashing
Bella at startup.
"""

from __future__ import annotations

import platform as _platform
import sys

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

PLATFORM_NAME = _platform.system() or sys.platform


def macos_backend():
    """Return the tools.platform.macos module, or None if not on macOS."""
    if not IS_MACOS:
        return None
    from tools.platform import macos
    return macos
