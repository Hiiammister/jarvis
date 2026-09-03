"""
bella.server.gateway — turns an abstract tool call into "run it in the right
place": on the brain (server-scoped tools) or on a specific device
(device-scoped tools), local or remote.

The runtime is handed a `DeviceGateway` as its `tool_gateway`. From the runtime's
point of view it looks exactly like `registry.execute` — `execute(name, args)`
returns a JSON string — so the agent loop is unchanged.

Concurrency: the agent loop runs on a worker thread; client connections live on
the asyncio loop. A remote device call hops threads via
`run_coroutine_threadsafe` and blocks the worker (which is fine — turns are
serialised anyway) until the `tool_result` arrives or the timeout fires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from bella.devices import (
    SERVER_DEVICE_ID,
    TOOL_CAPABILITY,
    DeviceLookup,
    TargetResolution,
    resolve_target_device,
)
from protocol import Envelope, new_request_id
from protocol.events import S

DEVICE_TOOL_TIMEOUT = 30.0  # seconds a client has to answer a device_tool_request


class _RegistryLookup(DeviceLookup):
    def __init__(self, registry):
        self._r = registry

    def find(self, alias_or_name: str):
        return self._r.find(alias_or_name)

    def get(self, device_id: str):
        return self._r.get(device_id)


class DeviceGateway:
    def __init__(self, registry, *, tool_timeout: float = DEVICE_TOOL_TIMEOUT):
        self.registry = registry
        self._lookup = _RegistryLookup(registry)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, tuple[asyncio.Future, str]] = {}  # req_id -> (fut, device_id)
        self.tool_timeout = tool_timeout

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- target resolution (used by the runtime, once per turn) ----------

    def resolve_target(self, text: str, source_device_id: str | None) -> TargetResolution:
        return resolve_target_device(text, source_device_id, self._lookup)

    # -- tool execution (the runtime's `tool_gateway.execute`) ----------

    def execute(self, name: str, arguments: dict[str, Any] | None, *,
                source_device_id: str | None = None,
                target_device_id: str | None = None) -> str:
        arguments = arguments or {}
        from tools.registry import get_registry

        reg = get_registry()
        tool = reg.get(name)
        scope = getattr(tool, "execution_scope", "server") if tool else "server"

        # Server-scoped tools always run on the brain.
        if tool is None or scope == "server":
            return reg.execute(name, arguments)

        # Device-scoped: pick where it runs.
        target_id = target_device_id or source_device_id or SERVER_DEVICE_ID
        target = self.registry.get(target_id)

        if target is None:
            return json.dumps({
                "error": f"Unknown device '{target_id}'. Connected: "
                         + ", ".join(d.device_name for d in self.registry.list(connected_only=True)),
                "offline": True,
            })

        if target.is_local:
            # The Mac running the brain is the target — run in-process.
            return reg.execute(name, arguments)

        if not target.connected or target.connection is None:
            return json.dumps({
                "error": f"{target.device_name} is currently offline — can't run "
                         f"'{name}' there.",
                "offline": True, "device": target.device_id,
            })

        cap = TOOL_CAPABILITY.get(name)
        if cap and not target.has_capability(cap):
            return json.dumps({
                "error": f"{target.device_name} can't do '{name}' "
                         f"(missing capability: {cap}).",
                "unsupported": True, "device": target.device_id,
            })

        return self._run_remote(target, name, arguments)

    # -- remote dispatch --------------------------------------------

    def _run_remote(self, device, name: str, arguments: dict) -> str:
        if self._loop is None:
            return json.dumps({"error": "server event loop not bound"})

        req_id = new_request_id()

        async def _do() -> str:
            fut: asyncio.Future = self._loop.create_future()
            self._pending[req_id] = (fut, device.device_id)
            env = Envelope.make(
                S.DEVICE_TOOL_REQUEST,
                {"tool": name, "args": arguments, "timeout": self.tool_timeout},
                request_id=req_id, device_id=device.device_id,
            )
            try:
                await device.connection.send(env)
            except Exception as e:
                self._pending.pop(req_id, None)
                return json.dumps({"error": f"could not reach {device.device_name}: {e}",
                                   "offline": True})
            try:
                payload = await asyncio.wait_for(fut, timeout=self.tool_timeout)
            except asyncio.TimeoutError:
                return json.dumps({
                    "error": f"{device.device_name} did not respond within "
                             f"{self.tool_timeout:.0f}s.",
                    "timeout": True, "device": device.device_id,
                })
            finally:
                self._pending.pop(req_id, None)

            if payload.get("ok"):
                return json.dumps(payload.get("result", {}), ensure_ascii=False)
            return json.dumps({"error": payload.get("error") or "device tool failed",
                               "device": device.device_id})

        cfut = asyncio.run_coroutine_threadsafe(_do(), self._loop)
        try:
            # generous outer bound — the inner wait_for is the real timeout
            return cfut.result(timeout=self.tool_timeout + 10)
        except Exception as e:
            return json.dumps({"error": f"device call failed: {e}", "device": device.device_id})

    def on_tool_result(self, request_id: str, payload: dict) -> None:
        """Called on the event loop by the websocket handler."""
        entry = self._pending.get(request_id)
        if entry is not None and not entry[0].done():
            entry[0].set_result(payload)

    def cancel_pending_for(self, device_id: str) -> None:
        """A device dropped — fail its in-flight tool calls instead of hanging."""
        for _req_id, (fut, did) in list(self._pending.items()):
            if did == device_id and not fut.done():
                fut.set_result({"ok": False, "error": "device disconnected"})
