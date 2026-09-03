"""
bella.server.auth — one place that decides whether a connection is allowed in.

V1 accepts the shared `JARVIS_TOKEN` (constant-time compared). The `AuthResult`
already carries a `device_id` and `permissions`, so per-device tokens can be
added later without touching any handler:

    authenticator.register_device_token(device_id, token, permissions=[...])

Handlers call `authenticator.authenticate(token)` — they never read the
environment or compare secrets themselves. Tokens are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

# Default grant for a client presenting the shared token.
_DEFAULT_PERMISSIONS = ["read", "write", "shell", "automation"]


@dataclass
class AuthResult:
    ok: bool
    device_id: str | None = None
    permissions: list[str] = field(default_factory=list)
    reason: str = ""

    def __bool__(self) -> bool:  # `if auth:` reads naturally
        return self.ok


@dataclass
class _DeviceToken:
    device_id: str
    token_sha256: str
    permissions: list[str]


class Authenticator:
    def __init__(self, shared_token: str | None):
        self._shared = shared_token or None
        self._device_tokens: dict[str, _DeviceToken] = {}  # sha256 -> record

    @property
    def enabled(self) -> bool:
        return bool(self._shared) or bool(self._device_tokens)

    @staticmethod
    def _sha(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register_device_token(self, device_id: str, token: str,
                              permissions: list[str] | None = None) -> None:
        digest = self._sha(token)
        self._device_tokens[digest] = _DeviceToken(
            device_id=device_id, token_sha256=digest,
            permissions=list(permissions or _DEFAULT_PERMISSIONS),
        )

    def authenticate(self, token: str | None) -> AuthResult:
        if not self.enabled:
            return AuthResult(False, reason="server has no token configured")
        if not token:
            return AuthResult(False, reason="missing token")

        digest = self._sha(token)

        rec = self._device_tokens.get(digest)
        if rec is not None:
            return AuthResult(True, device_id=rec.device_id,
                              permissions=list(rec.permissions))

        if self._shared and hmac.compare_digest(token, self._shared):
            return AuthResult(True, device_id=None,
                              permissions=list(_DEFAULT_PERMISSIONS))

        return AuthResult(False, reason="invalid token")

    def authenticate_bearer(self, header: str | None) -> AuthResult:
        """For HTTP `Authorization: Bearer <token>`."""
        if not header or not header.startswith("Bearer "):
            return AuthResult(False, reason="missing bearer token")
        return self.authenticate(header[len("Bearer "):].strip())
