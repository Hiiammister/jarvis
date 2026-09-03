"""
bella.server.websocket — the /ws connection handler.

One coroutine per connected client. Responsibilities:
  - authenticate the connection (token in ?token= or the hello payload)
  - register the device, ack with `connected`
  - pump client events; run `message` turns on the shared worker thread and
    stream assistant/tool events back to *this* client
  - forward `tool_result` frames to the gateway's pending futures
  - clean up on disconnect

Threading: a turn runs on `state.worker` (single thread, serialises Ollama +
SQLite). Its sync callbacks bridge back to this event loop via
`asyncio.run_coroutine_threadsafe` (fire-and-forget for stream chunks).
"""

from __future__ import annotations

import asyncio
import contextlib

from starlette.websockets import WebSocket, WebSocketDisconnect

from bella.server.device_registry import Connection
from bella.server.state import ServerState
from protocol import DeviceInfo, Envelope, decode, encode, is_client_event
from protocol.events import C, S
from protocol.models import ProtocolError

CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_HELLO = 4400


class WSConnection(Connection):
    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, env: Envelope) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self._ws.send_text(encode(env))
            except Exception:
                self.closed = True

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            await self._ws.close(code=code)


class _StreamBridge:
    """Sync callbacks (called from the worker thread) -> events to this client."""

    def __init__(self, loop, conn: WSConnection, request_id: str, device_id: str):
        self._loop = loop
        self._conn = conn
        self._rid = request_id
        self._did = device_id
        self._started = False

    def _emit(self, etype: str, payload: dict) -> None:
        env = Envelope.make(etype, payload, request_id=self._rid, device_id=self._did)
        asyncio.run_coroutine_threadsafe(self._conn.send(env), self._loop)

    def on_text(self, chunk: str) -> None:
        if not self._started:
            self._started = True
            self._emit(S.ASSISTANT_START, {})
        self._emit(S.ASSISTANT_CHUNK, {"text": chunk})

    def on_tool_start(self, name: str, args: dict) -> None:
        from bella.interfaces import theme

        self._emit(S.TOOL_START, {"tool": name, "title": theme.tool_title(name),
                                  "activity": theme.activity_label(name)})

    def on_tool_end(self, name: str, result_str: str) -> None:
        import json

        try:
            res = json.loads(result_str)
        except Exception:
            res = {}
        ok = not res.get("error")
        self._emit(S.TOOL_END, {"tool": name, "ok": ok,
                                "error": res.get("error", "") if not ok else ""})


async def ws_endpoint(ws: WebSocket, state: ServerState) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    conn = WSConnection(ws)
    device: DeviceInfo | None = None
    device_id = ""
    turn_tasks: set[asyncio.Task] = set()

    try:
        # -- 1. hello + auth --------------------------------------------
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
        try:
            env = decode(raw)
        except ProtocolError as e:
            await conn.send(Envelope.make(S.ERROR, {"message": f"bad frame: {e}"}))
            await conn.close(CLOSE_BAD_HELLO)
            return

        if env.type != C.HELLO:
            await conn.send(Envelope.make(S.ERROR, {"message": "first frame must be 'hello'"}))
            await conn.close(CLOSE_BAD_HELLO)
            return

        token = ws.query_params.get("token") or env.payload.get("token") or ""
        auth = state.authenticator.authenticate(token)
        if not auth:
            await conn.send(Envelope.make(S.ERROR, {"message": "unauthorized", "code": 401}))
            await conn.close(CLOSE_UNAUTHORIZED)
            return

        try:
            info = DeviceInfo.from_payload(env.payload)
        except ProtocolError as e:
            await conn.send(Envelope.make(S.ERROR, {"message": f"bad hello: {e}"}))
            await conn.close(CLOSE_BAD_HELLO)
            return

        # A per-device token pins the device_id; otherwise trust the hello id
        # only *after* auth has passed (never before).
        if auth.device_id:
            info.device_id = auth.device_id
        device = info
        device_id = info.device_id

        state.registry.register(info, conn, permissions=auth.permissions)
        await conn.send(Envelope.make(S.CONNECTED, {
            "device_id": device_id,
            "server": f"{state.config.ollama_model}",
            "protocol_version": state.protocol_version,
            "capabilities_ack": info.capabilities,
        }, device_id=device_id))

        # -- 2. event pump --------------------------------------------
        while True:
            raw = await ws.receive_text()
            try:
                env = decode(raw)
            except ProtocolError as e:
                await conn.send(Envelope.make(S.ERROR, {"message": f"bad frame: {e}"}))
                continue

            if not is_client_event(env.type):
                await conn.send(Envelope.make(S.ERROR, {"message": f"unknown event: {env.type}"}))
                continue

            state.registry.touch(device_id)

            if env.type == C.HEARTBEAT:
                await conn.send(Envelope.make(S.PING, {}, device_id=device_id))

            elif env.type == C.DISCONNECT:
                break

            elif env.type == C.DEVICE_CAPABILITIES:
                caps = env.payload.get("capabilities") or []
                state.registry.update_capabilities(device_id, list(caps))

            elif env.type == C.DEVICE_STATUS:
                dev = state.registry.get(device_id)
                if dev is not None:
                    dev.metadata["status"] = str(env.payload.get("state", ""))[:32]

            elif env.type == C.TOOL_RESULT:
                state.gateway.on_tool_result(env.request_id, env.payload)

            elif env.type == C.MESSAGE:
                # Run the turn as a task so the pump keeps reading — a device
                # tool call inside the turn needs its `tool_result` frame
                # delivered while the turn is still running.
                task = asyncio.create_task(_run_turn(env, conn, state, device_id, loop))
                turn_tasks.add(task)
                task.add_done_callback(turn_tasks.discard)

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 - never let one client kill the endpoint
        with contextlib.suppress(Exception):
            await conn.send(Envelope.make(S.ERROR, {"message": f"server error: {e}"}))
    finally:
        for task in list(turn_tasks):
            task.cancel()
        if device_id:
            state.registry.unregister(device_id, conn)
            state.gateway.cancel_pending_for(device_id)
        await conn.close()


async def _run_turn(env: Envelope, conn: WSConnection, state: ServerState,
                    device_id: str, loop) -> None:
    text = str(env.payload.get("text") or "").strip()
    rid = env.request_id or "r-ws"
    if not text:
        await conn.send(Envelope.make(S.ERROR, {"message": "empty message"}, request_id=rid))
        return
    if len(text) > 16_000:
        await conn.send(Envelope.make(S.ERROR, {"message": "message too long"}, request_id=rid))
        return

    bridge = _StreamBridge(loop, conn, rid, device_id)
    await conn.send(Envelope.make(S.THINKING, {"label": "thinking"},
                                  request_id=rid, device_id=device_id))

    state.request_started()

    def _do() -> object:
        return state.runtime.handle(
            text,
            source_device=device_id,
            on_text=bridge.on_text,
            on_tool_start=bridge.on_tool_start,
            on_tool_end=bridge.on_tool_end,
        )

    try:
        result = await loop.run_in_executor(state.worker, _do)
    except Exception as e:  # noqa: BLE001
        await conn.send(Envelope.make(S.ERROR, {"message": f"turn failed: {e}"}, request_id=rid))
        return
    finally:
        state.request_finished()

    payload = {
        "reply": result.reply,
        "route": result.route,
        "source": result.source,
        "target_device": result.target_device,
    }
    if result.source in ("local", "fast") and result.reply:
        # non-streamed — send the whole thing as one chunk so the client renders it
        await conn.send(Envelope.make(S.ASSISTANT_CHUNK, {"text": result.reply},
                                      request_id=rid, device_id=device_id))
    if result.source == "empty":
        await conn.send(Envelope.make(S.ERROR, {"message": "no usable response — try again"},
                                      request_id=rid))
        return
    await conn.send(Envelope.make(S.ASSISTANT_END, payload, request_id=rid, device_id=device_id))
