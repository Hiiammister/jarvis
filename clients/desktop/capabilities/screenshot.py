"""screenshot capability — grab the screen to a temp PNG. macOS (screencapture)
and Windows (via `mss`, if installed). Optional; only advertised when usable."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CAPABILITY = "screenshot"


def available() -> bool:
    if sys.platform == "darwin":
        return shutil.which("screencapture") is not None
    if sys.platform.startswith("win"):
        try:
            import mss  # noqa: F401

            return True
        except Exception:
            return False
    return False


def handle(tool: str, args: dict) -> dict:
    out = Path(tempfile.gettempdir()) / f"bella-shot-{int(time.time())}.png"
    try:
        if sys.platform == "darwin":
            subprocess.run(["screencapture", "-x", str(out)], capture_output=True, timeout=15)
        elif sys.platform.startswith("win"):
            import mss

            with mss.mss() as sct:
                sct.shot(output=str(out))
        else:
            return {"error": "screenshot not supported on this platform"}
        if out.exists():
            return {"success": True, "path": str(out), "bytes": out.stat().st_size}
        return {"error": "screenshot produced no file"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
