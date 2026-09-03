"""Auth, device registry, and target-device resolution — no network."""

import pytest

from bella.devices import SERVER_DEVICE_ID, resolve_target_device
from bella.server.auth import Authenticator
from bella.server.device_registry import DeviceRegistry
from protocol import DeviceInfo


# ── auth ─────────────────────────────────────────────────────────────────

def test_auth_shared_token():
    a = Authenticator("s3cr3t")
    assert a.authenticate("s3cr3t").ok
    assert not a.authenticate("nope").ok
    assert not a.authenticate("").ok
    assert not a.authenticate(None).ok


def test_auth_disabled_when_no_token():
    a = Authenticator(None)
    assert not a.enabled
    assert not a.authenticate("anything").ok


def test_auth_bearer():
    a = Authenticator("tok")
    assert a.authenticate_bearer("Bearer tok").ok
    assert not a.authenticate_bearer("tok").ok
    assert not a.authenticate_bearer(None).ok


def test_auth_per_device_token_future_ready():
    a = Authenticator("shared")
    a.register_device_token("windows-desktop", "dev-tok", permissions=["read", "shell"])
    r = a.authenticate("dev-tok")
    assert r.ok and r.device_id == "windows-desktop"
    assert r.permissions == ["read", "shell"]
    # shared still works too
    assert a.authenticate("shared").ok and a.authenticate("shared").device_id is None


# ── device registry ─────────────────────────────────────────────────────

def _info(**kw):
    base = dict(device_id="windows-desktop", device_name="Gaming PC", platform="windows",
                capabilities=["shell", "browser"], aliases=["windows", "pc", "gaming pc"])
    base.update(kw)
    return DeviceInfo(**base)


class _FakeConn:
    async def send(self, env): ...
    async def close(self, code=1000): ...


def test_registry_has_local_device():
    r = DeviceRegistry()
    local = r.get(SERVER_DEVICE_ID)
    assert local is not None and local.is_local and local.connected


def test_registry_register_and_find():
    r = DeviceRegistry()
    conn = _FakeConn()
    r.register(_info(), conn)
    assert r.get("windows-desktop").connected
    assert r.find("windows").device_id == "windows-desktop"
    assert r.find("gaming pc").device_id == "windows-desktop"
    assert r.find("pc").device_id == "windows-desktop"
    assert r.find("nonexistent") is None


def test_registry_unregister_marks_offline():
    r = DeviceRegistry()
    conn = _FakeConn()
    r.register(_info(), conn)
    r.unregister("windows-desktop", conn)
    assert not r.get("windows-desktop").connected
    # local device can't be removed
    r.unregister(SERVER_DEVICE_ID)
    assert r.get(SERVER_DEVICE_ID).connected


def test_registry_stale_replaced_connection_not_dropped():
    r = DeviceRegistry()
    c1, c2 = _FakeConn(), _FakeConn()
    r.register(_info(), c1)
    r.register(_info(), c2)          # reconnect
    r.unregister("windows-desktop", c1)  # stale close from the old socket
    assert r.get("windows-desktop").connected  # still up on c2


def test_has_capability():
    r = DeviceRegistry()
    r.register(_info(capabilities=["shell"]), _FakeConn())
    assert r.has_capability("windows-desktop", "shell")
    assert not r.has_capability("windows-desktop", "spotify")


# ── target resolution ──────────────────────────────────────────────────

class _Lookup:
    def __init__(self, reg): self.r = reg
    def find(self, q): return self.r.find(q)
    def get(self, d): return self.r.get(d)


@pytest.fixture
def lookup():
    r = DeviceRegistry()
    r.register(_info(), _FakeConn())
    return _Lookup(r)


def test_target_defaults_to_source(lookup):
    res = resolve_target_device("open vs code", "windows-desktop", lookup)
    assert res.device_id == "windows-desktop"
    assert not res.explicit


def test_target_explicit_other_device(lookup):
    res = resolve_target_device("open vs code on windows", "server-mac", lookup)
    assert res.device_id == "windows-desktop"
    assert res.explicit
    assert "on windows" not in res.cleaned_text.lower()


def test_target_mute_the_mac(lookup):
    res = resolve_target_device("mute the mac", "windows-desktop", lookup)
    assert res.device_id == "server-mac"
    assert res.explicit


def test_target_send_to_ipad_unknown(lookup):
    res = resolve_target_device("send this url to my ipad", "windows-desktop", lookup)
    # ipad not registered -> unresolved, text kept intact
    assert res.device_id is None and res.explicit


def test_target_generic_tail_not_a_device(lookup):
    for phrase in ["turn it on", "open this", "run it here", "focus on that"]:
        res = resolve_target_device(phrase, "windows-desktop", lookup)
        assert res.device_id == "windows-desktop"
        assert not res.explicit


def test_no_lookup_is_passthrough():
    res = resolve_target_device("open vs code on windows", "server-mac", None)
    assert res.device_id == "server-mac"
    assert res.cleaned_text == "open vs code on windows"


def test_topic_not_device_false_positives(lookup):
    # "windows 11" / "the mac" as topics, not targets
    for phrase in [
        "search for the latest news on windows 11",
        "what's the difference between windows and linux",
        "explain how to install python on windows",
        "look up reviews of the new mac studio",
    ]:
        res = resolve_target_device(phrase, "server-mac", lookup)
        assert res.device_id == "server-mac", phrase
        assert res.cleaned_text == phrase, phrase


def test_run_on_pc_alias(lookup):
    res = resolve_target_device("run git status on the gaming pc", "server-mac", lookup)
    assert res.device_id == "windows-desktop"
    res2 = resolve_target_device("run npm test on pc", "server-mac", lookup)
    assert res2.device_id == "windows-desktop"
