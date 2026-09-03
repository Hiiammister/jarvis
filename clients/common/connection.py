"""
clients.common.connection — a resilient WebSocket link to the Bella server.

Handles: connect, authenticate (token in the URL query), send `hello`, keep
alive with heartbeats, and reconnect with exponential backoff after any drop
(Wi-Fi change, server restart, laptop sleep). On every (re)connection it
re-sends `hello` so the server re-registers the device.

Transport only — it knows nothing about capabilities or tools. The caller
supplies an async `on_event(Envelope)` handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Awaitable, Callable

import websockets

from clients.common.protocol import C, DeviceInfo, Envelope, PROTOCOL_VERSION, decode, encode

HEARTBEAT_INTERVAL = 25.0
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0


class Connection:
    def __init__(
        self,
        server_url: str,
        token: str,
        device: DeviceInfo,
        on_event: Callable[[Envelope], Awaitable[None]],
        *,
        on_state: Callable[[str], None] | None = None,
    ):
        self._base = server_url.rstrip("/")
        self._token = token
        self._device = device
        self._on_event = on_event
        self._on_state = on_state or (lambda s: None)
        self._ws = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()

    # -- public --------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._connected.is_set()

    async def wait_ready(self) -> None:
        await self._connected.wait()

    async def send(self, env: Envelope) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        await ws.send(encode(env))

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._on_state(f"disconnected: {e}")
            if self._stop.is_set():
                break
            delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 0.5)
            attempt = min(attempt + 1, 8)
            self._on_state(f"reconnecting in {delay:.0f}s")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def stop(self) -> None:
        self._stop.set()

    # -- internals ---------------------------------------------------

    def _url(self) -> str:
        sep = "&" if "?" in self._base else "?"
        return f"{self._base}/ws{sep}token={self._token}"

    async def _session(self) -> None:
        self._on_state("connecting")
        async with websockets.connect(self._url(), max_size=2**20,
                                      ping_interval=20, ping_timeout=20) as ws:
            self._ws = ws
            try:
                await ws.send(encode(Envelope.make(C.HELLO, {
                    **self._device.to_dict(),
                    "protocol_version": PROTOCOL_VERSION,
                })))
                self._connected.set()
                self._on_state("connected")

                recv = asyncio.create_task(self._recv_loop(ws))
                beat = asyncio.create_task(self._heartbeat())
                stop = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {recv, beat, stop}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in done:
                    if task is not stop:
                        task.result()  # re-raise a real error
            finally:
                self._ws = None
                self._connected.clear()

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                env = decode(raw)
            except Exception:  # noqa: BLE001
                continue
            await self._on_event(env)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            with contextlib.suppress(Exception):
                await self.send(Envelope.make(C.HEARTBEAT, {}))
