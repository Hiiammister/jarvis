"""
bella.server.app — the FastAPI application: lifespan, HTTP routes, and the /ws
mount. Transport only; all brain logic is in BellaRuntime, all device logic in
the registry / gateway.

Routes:
  GET  /            the web client (web/index.html)
  GET  /health      lightweight, unauthenticated liveness + summary
  GET  /status      authenticated: model / devices / active requests
  GET  /devices     authenticated: connected device list
  POST /message     backward-compatible one-shot turn (bearer auth)
  WS   /ws          the preferred bidirectional protocol
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bella.config import REPO_ROOT, get_config
from bella.server.state import ServerState
from bella.server.websocket import ws_endpoint
from protocol import Envelope
from protocol.events import S

WEB_DIR = Path(REPO_ROOT) / "web"

_state: ServerState | None = None


def state() -> ServerState:
    assert _state is not None, "server not started"
    return _state


# ── lifespan ──────────────────────────────────────────────────────────────

async def _heartbeat_loop(st: ServerState) -> None:
    while True:
        await asyncio.sleep(30)
        gone = st.registry.prune_stale(max_idle=90)
        for did in gone:
            st.gateway.cancel_pending_for(did)


async def _broadcast_devices(st: ServerState) -> None:
    env = Envelope.make(S.DEVICES, {"devices": [d.public_dict() for d in st.registry.list()]})
    for d in st.registry.list(connected_only=True):
        if d.connection is not None:
            with contextlib.suppress(Exception):
                await d.connection.send(env)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    st = ServerState(get_config())
    _state = st
    loop = asyncio.get_running_loop()
    await st.startup(loop)
    st.registry.on_change(lambda: _broadcast_devices(st))
    hb = asyncio.create_task(_heartbeat_loop(st))
    _print_banner(st, loop)
    try:
        yield
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb
        await st.shutdown()
        _state = None


app = FastAPI(title="Bella", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ── auth helper ───────────────────────────────────────────────────────────

def _require_auth(authorization: str | None):
    res = state().authenticator.authenticate_bearer(authorization)
    if not res:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return res


# ── HTTP routes ───────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return state().health()


@app.get("/status")
def status(authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return state().status()


@app.get("/devices")
def devices(authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return {"devices": [d.public_dict() for d in state().registry.list()]}


class MessageIn(BaseModel):
    text: str


class MessageOut(BaseModel):
    reply: str
    route: str = ""
    target_device: str | None = None


@app.post("/message", response_model=MessageOut)
async def post_message(msg: MessageIn, authorization: str | None = Header(default=None)):
    auth = _require_auth(authorization)
    text = msg.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    st = state()
    src = auth.device_id or "http-client"
    st.request_started()

    def _do():
        return st.runtime.handle(text, source_device=src)

    try:
        result = await asyncio.get_running_loop().run_in_executor(st.worker, _do)
    finally:
        st.request_finished()

    if result.source == "empty":
        return MessageOut(reply="Sorry, I didn't get a usable response — try again.")
    return MessageOut(reply=result.reply, route=result.route, target_device=result.target_device)


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket):
    await ws_endpoint(websocket, state())


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return FileResponse(WEB_DIR / "chat.html")


@app.get("/chat.html")
def legacy_chat():
    return FileResponse(WEB_DIR / "chat.html")


@app.get("/{asset:path}")
def web_asset(asset: str):
    """Serve top-level web files (app.js, styles.css, manifest.json, sw.js) so
    the PWA works served from `/`. Whitelisted extensions only; never traverses
    out of web/."""
    allowed = {".js", ".css", ".json", ".png", ".svg", ".ico", ".webmanifest", ".map"}
    p = (WEB_DIR / asset).resolve()
    if WEB_DIR.resolve() in p.parents and p.suffix in allowed and p.is_file():
        return FileResponse(p)
    raise HTTPException(status_code=404)


# ── startup banner ────────────────────────────────────────────────────────

def _print_banner(st: ServerState, loop) -> None:
    from bella.server.net import lan_ip, tailscale_ip

    cfg = st.config
    verbose = cfg.verbose
    auth_state = "enabled" if st.authenticator.enabled else "DISABLED (no token!)"

    lines = [
        "",
        "BELLA SERVER",
        "",
        f"  Brain        {cfg.ollama_model}",
        f"  Memory       {st.health()['memory']}",
        "  WebSocket    ready",
        f"  Auth         {auth_state}",
        "",
        "  Listening",
    ]
    lan = lan_ip()
    ts = tailscale_ip()
    if lan:
        lines.append(f"    LAN        {lan}:{cfg.server_port}")
    if ts:
        lines.append(f"    Tailscale  {ts}:{cfg.server_port}")
    lines.append(f"    Local      localhost:{cfg.server_port}")
    lines.append("")
    lines.append("  Connected devices: 0")
    lines.append("")
    if cfg.server_host in ("127.0.0.1", "localhost"):
        lines.append("  Local-only mode. Set SERVER_HOST=0.0.0.0 for LAN/Tailscale clients.")
    elif not ts:
        lines.append("  Reachable on the LAN. Tailscale recommended for off-network access.")
    else:
        lines.append("  Reachable on LAN + Tailscale.")
    lines.append("")
    lines.append("  Waiting for Bella clients…")
    lines.append("")
    print("\n".join(lines), flush=True)
    if not verbose:
        print("  (uvicorn access logs hidden — BELLA_VERBOSE=1 to show)\n", flush=True)


# ── entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    import uvicorn

    cfg = get_config()
    if not cfg.auth_token:
        raise SystemExit(
            "JARVIS_TOKEN is not set in .env — refusing to start.\n"
            "This server can run shell commands on this machine and connected devices; "
            "it must not be reachable without a secret.\n"
            'Generate one:  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then add JARVIS_TOKEN=<value> to .env."
        )
    log_level = "info" if cfg.verbose else "warning"
    uvicorn.run(app, host=cfg.server_host, port=cfg.server_port,
                log_level=log_level, access_log=cfg.verbose)


if __name__ == "__main__":
    main()
