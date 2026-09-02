#!/usr/bin/env python3
"""
server.py — headless HTTP server exposing jarvis to other devices.

The REPL in jarvis.py and this server share the exact same brain: the same
fast-path routing (_fast_media_call / _fast_reminder_call), the same
build_system_prompt, the same tool executor. This file only adds a network
front door — it does not duplicate any tool-routing logic.

Run:
    python server.py

Then from another device (see README-REMOTE.md for the full setup —
Tailscale for Windows/iPad, Cloudflare Tunnel + Meta's Cloud API for
WhatsApp):

    curl -X POST http://<mac-hostname>:8765/message \\
         -H "Authorization: Bearer $JARVIS_TOKEN" \\
         -H "Content-Type: application/json" \\
         -d '{"text": "remind me to call mom at 5pm"}'

Or just open http://<mac-hostname>:8765/ in a browser (Windows, iPad, phone)
for a simple chat page.

SECURITY: every /message request must carry the JARVIS_TOKEN from .env as a
Bearer token. This process can run arbitrary shell commands, control apps,
and read/write files on this machine via jarvis's `shell` tool — it must
never be reachable without that check. There is no built-in rate limiting;
don't put this on the open internet without something in front of it.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jarvis import (
    build_system_prompt,
    _trim_messages,
    _extract_and_save_memory,
    _spawn_bg,
    _fast_media_call,
    _fast_reminder_call,
    _fast_media_reply,
)
from memory.core import Session
from llm.ollama_client import run_agent_turn
from tools.executor import execute_tool

JARVIS_TOKEN = os.getenv("JARVIS_TOKEN")
if not JARVIS_TOKEN:
    raise SystemExit(
        "JARVIS_TOKEN is not set in .env — refusing to start.\n"
        "This server can run shell commands on this machine; it must not be "
        "reachable without a secret.\n"
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
        "then add JARVIS_TOKEN=<that value> to .env."
    )

HOST = os.getenv("SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("SERVER_PORT", "8765"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Must run on the same pinned thread every /message call will use.
    _worker.submit(_init_session).result()
    yield


app = FastAPI(title="Bella", lifespan=lifespan)

# One shared conversation across every device — you, talking to one
# assistant, regardless of whether you're on the Mac, Windows, iPad, or
# WhatsApp.
#
# Every request runs on this single dedicated worker thread. Two reasons:
# 1. sqlite3 connections (Session) are only usable from the thread that
#    created them — FastAPI's default threadpool hands each request a
#    different thread, which breaks that. Pinning to one thread fixes it
#    without touching the shared memory/core.py.
# 2. It naturally serializes turns, which Ollama requires anyway (it only
#    handles one request at a time — see jarvis-architecture memory note).
_worker = ThreadPoolExecutor(max_workers=1)
_messages: list[dict] = []
_session: Session | None = None


def _init_session() -> None:
    global _session
    _session = Session()


class MessageIn(BaseModel):
    text: str


class MessageOut(BaseModel):
    reply: str


def _check_auth(authorization: str | None) -> None:
    if not authorization or authorization != f"Bearer {JARVIS_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def handle_message(user_input: str) -> str:
    """The same routing jarvis.py's REPL uses (_handle_input), minus the
    Rich console/spinner output: fast path first, else the full agent loop."""
    global _messages

    fast = _fast_media_call(user_input) or _fast_reminder_call(user_input)
    if fast:
        tool, inp = fast
        _session.add_message("user", user_input)
        result_str = execute_tool(tool, inp)
        try:
            result = json.loads(result_str)
        except Exception:
            result = {"raw": result_str}
        reply = _fast_media_reply(tool, inp, result)
        _session.add_message("assistant", reply)
        _messages = _messages + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply},
        ]
        return reply

    _session.add_message("user", user_input)
    working = _messages + [{"role": "user", "content": user_input}]
    system = build_system_prompt()

    reply_text, _ = run_agent_turn(
        messages=_trim_messages(working),
        system=system,
        on_text=lambda t: None,
        on_tool_start=lambda name, inputs: None,
        on_tool_end=lambda name, result: None,
    )

    if not reply_text:
        # Model produced nothing usable — don't record a dangling user turn.
        return "Sorry, I didn't get a usable response from the model — try again."

    _session.add_message("assistant", reply_text)
    _spawn_bg(_extract_and_save_memory, user_input, reply_text)
    _messages = working + [{"role": "assistant", "content": reply_text}]
    return reply_text


@app.post("/message", response_model=MessageOut)
async def post_message(msg: MessageIn, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    text = msg.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    loop = asyncio.get_running_loop()
    reply = await loop.run_in_executor(_worker, handle_message, text)
    return MessageOut(reply=reply)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def chat_page():
    return FileResponse(Path(__file__).parent / "web" / "chat.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
