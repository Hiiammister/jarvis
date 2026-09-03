"""
bella.server.device_registry — the single source of truth for connected devices.

Nothing outside this module should hold a raw WebSocket. Handlers register a
`Connection` (a thin send/close duck type) and everything else works through
`Device` objects and lookups.

The `server-mac` device is always present (the machine running the brain); it
has no connection and its tools run in-process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bella.devices import SERVER_DEVICE_ID, local_capabilities, local_hostname, platform_key
from protocol import DeviceInfo, Envelope


class Connection:
    """Duck type for a live client link. The websocket handler supplies one."""

    async def send(self, env: Envelope) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class Device:
    device_id: str
    device_name: str
    platform: str
    client_type: str = "desktop"
    hostname: str = ""
    capabilities: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    connection: Connection | None = None
    connected: bool = False
    last_seen: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- queries -------------------------------------------------------

    @property
    def is_local(self) -> bool:
        return self.device_id == SERVER_DEVICE_ID

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    def all_names(self) -> set[str]:
        names = {self.device_id.lower(), self.device_name.lower()}
        names |= {a.lower() for a in self.aliases}
        names |= {w for n in (self.device_name.lower(),) for w in n.split()}
        names.add(self.platform.lower())
        return {n for n in names if n}

    def touch(self) -> None:
        self.last_seen = time.time()

    def public_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "platform": self.platform,
            "client_type": self.client_type,
            "capabilities": list(self.capabilities),
            "connected": self.connected,
            "last_seen": round(self.last_seen, 1),
            "is_local": self.is_local,
        }


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._on_change: list[Callable[[], Awaitable[None]]] = []
        self._ensure_local()

    # -- change notification (websocket layer pushes `devices` on change) --

    def on_change(self, cb: Callable[[], Awaitable[None]]) -> None:
        self._on_change.append(cb)

    def _changed(self) -> None:
        import asyncio

        for cb in self._on_change:
            try:
                asyncio.get_running_loop().create_task(cb())
            except RuntimeError:
                pass  # no running loop (tests / startup) — fine

    # -- the local machine -------------------------------------------

    def _ensure_local(self) -> None:
        if SERVER_DEVICE_ID in self._devices:
            return
        self._devices[SERVER_DEVICE_ID] = Device(
            device_id=SERVER_DEVICE_ID,
            device_name=f"{local_hostname()} (server)",
            platform=platform_key(),
            client_type="server",
            hostname=local_hostname(),
            capabilities=local_capabilities(),
            aliases=["mac", "macbook", "server", "this mac", "here", "local"],
            permissions=["read", "write", "shell", "automation"],
            connected=True,
        )

    # -- registration ----------------------------------------------

    def register(self, info: DeviceInfo, connection: Connection,
                 permissions: list[str] | None = None) -> Device:
        existing = self._devices.get(info.device_id)
        dev = existing or Device(device_id=info.device_id, device_name=info.device_name,
                                 platform=info.platform)
        dev.device_name = info.device_name
        dev.platform = info.platform
        dev.client_type = info.client_type
        dev.hostname = info.hostname
        dev.capabilities = list(info.capabilities)
        dev.aliases = list(info.aliases)
        if permissions is not None:
            dev.permissions = list(permissions)
        dev.connection = connection
        dev.connected = True
        dev.touch()
        self._devices[dev.device_id] = dev
        self._changed()
        return dev

    def unregister(self, device_id: str, connection: Connection | None = None) -> None:
        dev = self._devices.get(device_id)
        if dev is None or dev.is_local:
            return
        # Ignore a stale close from a connection that's already been replaced.
        if connection is not None and dev.connection is not connection:
            return
        dev.connected = False
        dev.connection = None
        dev.touch()
        self._changed()

    def touch(self, device_id: str) -> None:
        dev = self._devices.get(device_id)
        if dev is not None:
            dev.touch()

    def update_capabilities(self, device_id: str, capabilities: list[str]) -> None:
        dev = self._devices.get(device_id)
        if dev is not None:
            dev.capabilities = [str(c)[:32] for c in capabilities][:32]
            dev.touch()
            self._changed()

    # -- lookups --------------------------------------------------

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def list(self, *, connected_only: bool = False) -> list[Device]:
        out = list(self._devices.values())
        if connected_only:
            out = [d for d in out if d.connected]
        return out

    def find(self, alias_or_name: str) -> Device | None:
        """Name / alias match for conversational targeting. Deliberately exact
        (device_id, device_name, an advertised alias, or the platform word) —
        no substring or fuzzy matching, so "news on windows 11" never resolves
        to the Windows device. Favours a missed target over a wrong one."""
        q = alias_or_name.strip().lower()
        if not q:
            return None
        if q in self._devices:
            return self._devices[q]
        for d in self._devices.values():
            if q in d.all_names():
                return d
        return None

    def has_capability(self, device_id: str, cap: str) -> bool:
        dev = self._devices.get(device_id)
        return bool(dev and dev.has_capability(cap))

    def prune_stale(self, max_idle: float = 90.0) -> list[str]:
        """Mark connected-but-silent remote devices as gone. Returns their ids."""
        now = time.time()
        gone: list[str] = []
        for d in self._devices.values():
            if d.is_local or not d.connected:
                continue
            if now - d.last_seen > max_idle:
                d.connected = False
                d.connection = None
                gone.append(d.device_id)
        if gone:
            self._changed()
        return gone
