"""browser capability — open a URL in the local default browser. Cross-platform."""

from __future__ import annotations

import webbrowser

CAPABILITY = "browser"


def handle(tool: str, args: dict) -> dict:
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "no url"}
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("file://")):
        url = "https://" + url
    try:
        ok = webbrowser.open(url, new=2)
        return {"success": bool(ok), "url": url}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
