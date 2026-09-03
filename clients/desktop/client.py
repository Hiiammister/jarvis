"""
clients.desktop.client — the Bella desktop client (macOS / Windows / Linux).

It is BOTH:
  - a chat client   (type to talk to the shared Bella; replies stream in)
  - a tool executor (runs device_tool_request from the server: shell, browser,
    filesystem, notifications, screenshot)

There is no model here. All intelligence, memory, and conversation state live on
the Bella server. This process just connects, advertises what the machine can
do, and does it when asked.

    python -m clients.desktop.client --server ws://mac.local:8765 --token XXXX
    # or set BELLA_SERVER / BELLA_TOKEN and just:
    python -m clients.desktop.client
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import re
import socket
import sys

from clients.common.connection import Connection
from clients.common.protocol import C, DeviceInfo, Envelope, S, is_server_event
from clients.desktop.capabilities import build as build_caps
from clients.desktop.capabilities import dispatch

TEAL = "\033[38;2;94;234;212m"
DIM = "\033[38;2;107;114;128m"
RESET = "\033[0m"


def _platform_key() -> str:
    p = sys.platform
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    if p.startswith("linux"):
        return "linux"
    return "unknown"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "device"


def _default_device(name: str | None) -> tuple[str, str]:
    host = socket.gethostname().split(".")[0]
    display = name or f"{host}"
    device_id = f"{_platform_key()}-{_slug(host)}"
    return device_id, display


def _aliases(display: str) -> list[str]:
    plat = _platform_key()
    base = {plat, display.lower(), *display.lower().split()}
    if plat == "windows":
        base |= {"windows", "pc", "desktop", "the pc"}
    elif plat == "macos":
        base |= {"mac", "macbook", "laptop"}
    elif plat == "linux":
        base |= {"linux", "the linux box"}
    return sorted(a for a in base if a)


class DesktopClient:
    def __init__(self, server: str, token: str, name: str | None,
                 disable: set[str], quiet: bool):
        self.quiet = quiet
        caps, self.handlers = build_caps(disable)
        device_id, display = _default_device(name)
        self.device = DeviceInfo(
            device_id=device_id,
            device_name=display,
            platform=_platform_key(),
            client_type="desktop",
            hostname=socket.gethostname(),
            capabilities=caps,
            aliases=_aliases(display),
        )
        self.conn = Connection(server, token, self.device, self._on_event,
                               on_state=self._on_state)
        self._streaming = False

    # -- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        self._banner()
        await asyncio.gather(self.conn.run(), self._stdin_loop())

    def _banner(self) -> None:
        print(f"{TEAL}BELLA{RESET}  desktop client")
        print(f"{DIM}  device   {self.device.device_name}  ({self.device.device_id}){RESET}")
        print(f"{DIM}  can do   {', '.join(self.device.capabilities)}{RESET}")
        print(f"{DIM}  type to talk to Bella · Ctrl-C to quit{RESET}\n")

    def _on_state(self, state: str) -> None:
        if not self.quiet:
            print(f"{DIM}· {state}{RESET}")

    # -- inbound events ----------------------------------------------

    async def _on_event(self, env: Envelope) -> None:
        if not is_server_event(env.type):
            return
        t = env.type
        p = env.payload

        if t == S.CONNECTED:
            print(f"{DIM}· connected to Bella ({p.get('server','')}){RESET}")

        elif t == S.DEVICE_TOOL_REQUEST:
            await self._run_device_tool(env)

        elif t == S.ASSISTANT_START:
            self._streaming = True
            sys.stdout.write(f"{TEAL}Bella{RESET} ")
            sys.stdout.flush()

        elif t == S.ASSISTANT_CHUNK:
            sys.stdout.write(p.get("text", ""))
            sys.stdout.flush()

        elif t == S.ASSISTANT_END:
            if self._streaming or p.get("reply"):
                if not self._streaming and p.get("reply"):
                    sys.stdout.write(f"{TEAL}Bella{RESET} " + p["reply"])
                sys.stdout.write("\n")
            self._streaming = False
            self._prompt()

        elif t == S.TOOL_START:
            print(f"\n{DIM}  ◈ {p.get('activity', p.get('tool',''))}…{RESET}")

        elif t == S.TOOL_END:
            mark = "✓" if p.get("ok") else "✗"
            extra = "" if p.get("ok") else f"  {p.get('error','')}"
            print(f"{DIM}  {mark} {p.get('tool','')}{extra}{RESET}")

        elif t == S.THINKING:
            pass

        elif t == S.NOTIFICATION:
            dispatch(self.handlers, "notification", p) if "notifications" in self.handlers else None
            print(f"{DIM}⚑ {p.get('title','')}: {p.get('body','')}{RESET}")

        elif t == S.ERROR:
            print(f"\n\033[38;2;248;113;113m✗ {p.get('message','error')}{RESET}")
            self._prompt()

        elif t == S.DEVICES:
            n = sum(1 for d in p.get("devices", []) if d.get("connected"))
            if not self.quiet:
                print(f"{DIM}· {n} device(s) connected{RESET}")

        elif t == S.PING:
            pass

    async def _run_device_tool(self, env: Envelope) -> None:
        tool = env.payload.get("tool", "")
        args = env.payload.get("args", {}) or {}
        if not self.quiet:
            print(f"{DIM}· running {tool} locally…{RESET}")
        result = await asyncio.get_running_loop().run_in_executor(
            None, dispatch, self.handlers, tool, args
        )
        ok = not result.get("error")
        await self.conn.send(Envelope.make(
            C.TOOL_RESULT,
            {"ok": ok, "tool": tool, "result": result if ok else {},
             "error": result.get("error", "") if not ok else ""},
            request_id=env.request_id,
        ))

    # -- outbound: stdin -> message --------------------------------

    def _prompt(self) -> None:
        sys.stdout.write(f"{DIM}You ›{RESET} ")
        sys.stdout.flush()

    async def _stdin_loop(self) -> None:
        loop = asyncio.get_running_loop()
        await self.conn.wait_ready()
        self._prompt()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF
                self.conn.stop()
                return
            text = line.strip()
            if not text:
                self._prompt()
                continue
            if text in ("/quit", "/exit"):
                self.conn.stop()
                return
            if not self.conn.ready:
                print(f"{DIM}· reconnecting — hold on…{RESET}")
                await self.conn.wait_ready()
            try:
                await self.conn.send(Envelope.make(C.MESSAGE, {"text": text}))
            except Exception as e:  # noqa: BLE001
                print(f"{DIM}· send failed ({e}){RESET}")
                self._prompt()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bella-client", description="Bella desktop client")
    ap.add_argument("--server", default=os.getenv("BELLA_SERVER", "ws://localhost:8765"),
                    help="ws://host:port of the Bella server (env: BELLA_SERVER)")
    ap.add_argument("--token", default=os.getenv("BELLA_TOKEN", os.getenv("JARVIS_TOKEN", "")),
                    help="auth token (env: BELLA_TOKEN / JARVIS_TOKEN)")
    ap.add_argument("--name", default=os.getenv("BELLA_DEVICE_NAME"),
                    help="friendly device name (default: hostname)")
    ap.add_argument("--disable", default="", help="comma-separated capabilities to not advertise")
    ap.add_argument("--quiet", action="store_true", help="less connection chatter")
    ns = ap.parse_args(argv)

    if not ns.token:
        print("no token — pass --token or set BELLA_TOKEN", file=sys.stderr)
        return 2
    server = ns.server
    if server.startswith("http://"):
        server = "ws://" + server[7:]
    elif server.startswith("https://"):
        server = "wss://" + server[8:]
    elif not server.startswith(("ws://", "wss://")):
        server = "ws://" + server

    client = DesktopClient(server, ns.token, ns.name,
                           {c.strip() for c in ns.disable.split(",") if c.strip()},
                           ns.quiet)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
