"""
clients.desktop.capabilities — what this machine can do, loaded conditionally.

`build()` returns (advertised_capabilities, handler_map). The handler map is
{capability: fn(tool, args) -> result_dict}; the tool -> capability mapping
matches the server's bella.devices.TOOL_CAPABILITY.

Windows never imports macOS-only modules and vice versa — each capability
module is stdlib-only and branches internally on sys.platform.
"""

from __future__ import annotations

from typing import Callable

from clients.desktop.capabilities import browser, filesystem, notifications, screenshot, shell

# device tool name -> capability
TOOL_CAPABILITY = {
    "shell": "shell",
    "read_file": "filesystem",
    "write_file": "filesystem",
    "open_browser": "browser",
    "screenshot": "screenshot",
}

Handler = Callable[[str, dict], dict]


def build(extra_disable: set[str] | None = None) -> tuple[list[str], dict[str, Handler]]:
    disable = extra_disable or set()
    handlers: dict[str, Handler] = {}
    caps: list[str] = []

    for mod in (shell, browser, filesystem, notifications):
        if mod.CAPABILITY in disable:
            continue
        handlers[mod.CAPABILITY] = mod.handle
        caps.append(mod.CAPABILITY)

    if "screenshot" not in disable and screenshot.available():
        handlers["screenshot"] = screenshot.handle
        caps.append("screenshot")

    return caps, handlers


def dispatch(handlers: dict[str, Handler], tool: str, args: dict) -> dict:
    cap = TOOL_CAPABILITY.get(tool)
    if cap is None:
        return {"error": f"this device has no handler for '{tool}'"}
    fn = handlers.get(cap)
    if fn is None:
        return {"error": f"this device can't do '{tool}' (capability {cap} unavailable)"}
    try:
        return fn(tool, args)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{tool} failed: {e}"}
