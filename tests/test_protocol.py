"""protocol encode/decode + validation."""

import pytest

from protocol import Envelope, decode, encode
from protocol.events import is_client_event, is_server_event
from protocol.models import DeviceInfo, ProtocolError


def test_roundtrip():
    e = Envelope.make("message", {"text": "hi"}, request_id="r-1", device_id="pc")
    d = decode(encode(e))
    assert d.type == "message"
    assert d.payload == {"text": "hi"}
    assert d.request_id == "r-1"
    assert d.device_id == "pc"
    assert d.v == 1


@pytest.mark.parametrize("raw", [
    "not json", "[]", '{"payload":{}}', '{"type":""}',
    '{"type":"x","v":2}', '{"type":"x","payload":5}',
])
def test_malformed_rejected(raw):
    with pytest.raises(ProtocolError):
        decode(raw)


def test_oversized_rejected():
    with pytest.raises(ProtocolError):
        decode('{"type":"x","payload":{"a":"' + "z" * 300_000 + '"}}')


def test_event_sets_disjoint_and_known():
    assert is_client_event("hello") and not is_server_event("hello")
    assert is_server_event("assistant_chunk") and not is_client_event("assistant_chunk")
    assert not is_client_event("nonsense")


def test_deviceinfo_requires_id():
    with pytest.raises(ProtocolError):
        DeviceInfo.from_payload({"device_name": "X"})
    di = DeviceInfo.from_payload({"device_id": "pc", "capabilities": ["shell", "browser"]})
    assert di.device_id == "pc"
    assert di.capabilities == ["shell", "browser"]
    assert di.protocol_version == 1


def test_deviceinfo_truncates_hostile_input():
    di = DeviceInfo.from_payload({
        "device_id": "x" * 200,
        "capabilities": ["c" * 100] * 200,
        "aliases": ["A" * 100] * 100,
    })
    assert len(di.device_id) == 64
    assert len(di.capabilities) == 32
    assert all(len(c) <= 32 for c in di.capabilities)
    assert all(a.islower() for a in di.aliases)
