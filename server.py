#!/usr/bin/env python3
"""
server.py — thin entry point for the headless HTTP interface (kept for
compatibility with com.jarvis.server.plist and existing docs).

The implementation is in `bella.server`. It drives the same `BellaRuntime` the
CLI uses — same fast-path routing, same system prompt, same tools, same memory.

Run:
    python server.py          # or:  bin/bella serve

See README-REMOTE.md for the full remote-access setup (Tailscale, Cloudflare
Tunnel, WhatsApp). SECURITY: /message requires JARVIS_TOKEN as a Bearer token —
this process can run arbitrary shell commands.
"""

from bella.server import app, main  # noqa: F401  (uvicorn imports `app`)

if __name__ == "__main__":
    main()
