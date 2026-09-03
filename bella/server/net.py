"""bella.server.net — LAN / Tailscale address detection for the startup banner."""

from __future__ import annotations

import socket


def lan_ip() -> str | None:
    """The address other LAN devices would use to reach this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


def tailscale_ip() -> str | None:
    """A 100.64.0.0/10 (CGNAT) address == a Tailscale interface."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for _f, _t, _p, _c, sockaddr in infos:
            ip = sockaddr[0]
            first, second = (int(x) for x in ip.split(".")[:2])
            if first == 100 and 64 <= second <= 127:
                return ip
    except Exception:
        pass
    # fall back to scanning interface addresses via a UDP connect to a TS-range host
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("100.100.100.100", 80))
        ip = s.getsockname()[0]
        s.close()
        first, second = (int(x) for x in ip.split(".")[:2])
        if first == 100 and 64 <= second <= 127:
            return ip
    except Exception:
        pass
    return None
