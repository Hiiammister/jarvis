"""
bella.server — the Bella hub: FastAPI transport + WebSocket protocol + device
registry, all driving the single shared BellaRuntime.

  bella.server.app              FastAPI app + HTTP routes + startup banner
  bella.server.websocket        /ws connection handler
  bella.server.device_registry  DeviceRegistry / Device
  bella.server.gateway          DeviceGateway — server vs device tool routing
  bella.server.auth             Authenticator — token check abstraction
  bella.server.state            ServerState — process singletons
"""

from bella.server.app import app, main

__all__ = ["app", "main"]
