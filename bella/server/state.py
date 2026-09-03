"""
bella.server.state — the process-wide server singletons, created once at
startup and shared by every request.

  runtime        the ONE BellaRuntime (shared brain / memory / model)
  registry       DeviceRegistry (connected clients + server-mac)
  gateway        DeviceGateway (server vs device tool routing)
  authenticator  Authenticator (token check)
  worker         single-thread executor — serialises turns, pins SQLite

The BellaRuntime and its SQLite session are created ON the worker thread
(sqlite3 objects are thread-affine), so `init()` must be submitted there.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from bella.config import BellaConfig, get_config
from bella.server.auth import Authenticator
from bella.server.device_registry import DeviceRegistry
from bella.server.gateway import DeviceGateway
from protocol import PROTOCOL_VERSION


class ServerState:
    def __init__(self, config: BellaConfig | None = None):
        self.config = config or get_config()
        self.started_at = time.time()
        self.protocol_version = PROTOCOL_VERSION

        self.authenticator = Authenticator(self.config.auth_token)
        self.registry = DeviceRegistry()
        self.gateway = DeviceGateway(self.registry)

        # single worker: one Ollama turn at a time, SQLite pinned to it
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bella-turn")
        self.runtime = None  # set by init() on the worker thread
        self._active_requests = 0

    # -- lifecycle ---------------------------------------------------

    def init_runtime(self) -> None:
        """Runs on the worker thread (SQLite is thread-affine)."""
        from bella.runtime import BellaRuntime

        self.runtime = BellaRuntime(config=self.config, tool_gateway=self.gateway)

    async def startup(self, loop) -> None:
        self.gateway.bind_loop(loop)
        self.worker.submit(self.init_runtime).result()

    async def shutdown(self) -> None:
        if self.runtime is not None:
            try:
                self.worker.submit(self.runtime.shutdown).result(timeout=25)
            except Exception:
                pass
        self.worker.shutdown(wait=False, cancel_futures=True)

    # -- misc ------------------------------------------------------

    @property
    def uptime(self) -> float:
        return round(time.time() - self.started_at, 1)

    def request_started(self) -> None:
        self._active_requests += 1

    def request_finished(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)

    @property
    def active_requests(self) -> int:
        return self._active_requests

    # -- summaries -----------------------------------------------

    def health(self) -> dict:
        return {
            "status": "ok",
            "model": self.config.ollama_model,
            "memory": "ready",
            "connected_devices": len(self.registry.list(connected_only=True)),
            "uptime": self.uptime,
            "protocol_version": self.protocol_version,
        }

    def status(self) -> dict:
        from tools.registry import get_registry

        return {
            "model": self.config.ollama_model,
            "provider": "Ollama",
            "uptime": self.uptime,
            "tool_count": len(get_registry().available()),
            "memory": "ready",
            "active_requests": self.active_requests,
            "protocol_version": self.protocol_version,
            "devices": [d.public_dict() for d in self.registry.list()],
        }
