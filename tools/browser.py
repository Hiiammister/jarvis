"""tools/browser.py — open a URL in the user's default browser."""

from __future__ import annotations

import subprocess
import sys
import webbrowser

from tools.base import PermissionLevel, Tool
from tools.registry import register


def run_open_browser(url: str) -> dict:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
        else:
            # Linux / other — webbrowser handles xdg-open, and it's cross-platform.
            if not webbrowser.open(url):
                subprocess.Popen(["xdg-open", url])
        return {"success": True, "url": url}
    except Exception as e:
        return {"error": str(e)}


class OpenBrowserTool(Tool):
    name = "open_browser"
    execution_scope = "device"
    description = (
        "Open a URL in the user's default browser. Use this whenever the user "
        "wants to visit a website, open a link, or navigate to a page."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to open."},
        },
        "required": ["url"],
    }
    permission = PermissionLevel.LOW_RISK

    def execute(self, url: str) -> dict:
        return run_open_browser(url)


register(OpenBrowserTool())
