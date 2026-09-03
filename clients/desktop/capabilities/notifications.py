"""notifications capability — a local desktop notification. Per-platform, no deps."""

from __future__ import annotations

import shutil
import subprocess
import sys

CAPABILITY = "notifications"


def _osascript_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def handle(tool: str, args: dict) -> dict:
    title = str(args.get("title") or "Bella")
    body = str(args.get("body") or args.get("message") or args.get("text") or "")
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_osascript_str(body)}" with title "{_osascript_str(title)}"'],
                capture_output=True, timeout=10,
            )
        elif sys.platform.startswith("win"):
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null; "
                f"[console]::WriteLine('{title}: {body}')"
            )
            # Best-effort toast; falls back to a msg box.
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"[System.Windows.Forms.MessageBox]::Show('{body}','{title}')"
                            if not shutil.which("wt") else ps],
                           capture_output=True, timeout=10)
        else:
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", title, body], capture_output=True, timeout=10)
            else:
                return {"error": "no notification backend (install libnotify)"}
        return {"success": True, "title": title}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
