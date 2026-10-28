"""The `dmr` decoder through shack_sdr's Python face, as Radio drives it:
the catalogue's entry and its partial availability without mbelib, a
session fed the test transmitter's repeater (`_native.testsig_dmr`'s
showcase: both slots busy at once) with calls, talker alias, an encrypted
call and an SMS coming back as results and the slots' summary as meta,
the session saying needs_codec:mbelib during a call and decoding
everything else, and - only with a developer's own mbelib named by
SHACK_TEST_MBELIB, never in the repository - sound pulled at Radio's rate."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

import shack_sdr
from shack_sdr import DecoderError, Host, _native, codecs

T0 = 1_800_000_000.0
RATE = 48_000.0


@pytest.fixture
def no_mbelib(monkeypatch, tmp_path):
    """This process's loader sees no mbelib at all."""
    monkeypatch.delenv("SHACK_CODEC_MBELIB", raising=False)
    monkeypatch.delenv("SHACK_HOSTLIBS", raising=False)
    monkeypatch.setattr(codecs.Loader, "_system", staticmethod(lambda names: []))
    codecs.configure(tmp_path / "codecs", cache_path=tmp_path / "cache.json")
    codecs.publish()
    yield
    _native.codec_use("mbelib", None)


def iq(**kw) -> np.ndarray:
    return np.frombuffer(_native.testsig_dmr(**kw), np.complex64)


def feed(host: Host, sid: str, x: np.ndarray, rate: float = RATE, start: int = 0) -> int:
    block = int(rate / 10)
    for k in range(0, len(x), block):
        host.feed(sid, x[k:k + block], T0 + (start + k) / rate, start + k)
    return start + len(x)


def collect(host: Host, sid: str, until, secs: float = 20.0) -> list[dict]:
    got: list[dict] = []
    end = time.monotonic() + secs
    while time.monotonic() < end:
        got += [i for i in host.poll(500) if i["session"] == sid]
        if until(got):
            break
        time.sleep(0.02)
    return got


def frames(items: list[dict], kind: str) -> list[dict]:
    return [i["fields"] for i in items if i["type"] == "frame" and i["frame"] == kind]


def test_the_catalogue_offers_dmr_without_mbelib_for_all_but_voice(no_mbelib):
    e = next(e for e in shack_sdr.catalogue() if e["id"] == "dmr")
    assert e["family"] == "digital_voice" and e["inputs"] == ["iq"] and e["panel"] == "slots"
    assert e["rateHz"] == 48_000 and e["bandwidthHz"] == 12_500 and e["minRateHz"] == 16_000
    assert e["needs"] == ["mbelib"] and e["needsFor"] == {"mbelib": ["voice"]}
    assert e["available"] and e["why"] is None and e["partial"] and e["missing"] == ["mbelib"]
    assert {p["key"]: (p["type"], p["default"]) for p in e["params"]} == {"slot": ("text", "both")}
    assert "dmr" in next(s for s in codecs.status() if s["id"] == "mbelib")["for"]


def test_a_repeater_with_both_slots_busy_is_followed(no_mbelib):
    x = iq(snr_db=18.0, offset_hz=800.0)
    # The input ring holds all 7.7 s fed at once (a radio feeds in real time).
    with Host(cpu_max=0.0, ring_s=12.0) as host:
        sid = host.start("dmr", {}, "iq", RATE)
        at = feed(host, sid, x)
        feed(host, sid, np.zeros(int(RATE), np.complex64), start=at)
        got = collect(host, sid, lambda g: len(frames(g, "call_end")) >= 3 and frames(g, "sms"))
        ends = {(f["slot"], f["src"]): f for f in frames(got, "call_end")}
        assert set(ends) == {(1, 9_990_001), (2, 9_990_002), (2, 9_990_003)}
        tg, private, secret = ends[1, 9_990_001], ends[2, 9_990_002], ends[2, 9_990_003]
        assert (tg["dst"], tg["callType"], tg["alias"], tg["encrypted"]) == (99, "group", "MX0TST Test", False)
        assert (private["dst"], private["callType"], private["alias"]) == (9_990_001, "private", "MX0ABC")
        assert secret["encrypted"] is True and secret["dst"] == 98
        assert 2.9 < tg["durationS"] < 3.1 and tg["ber"] < 0.02 and tg["rssiDb"] < 0
        assert [f["alias"] for f in frames(got, "talker_alias")] == ["MX0TST Test", "MX0ABC"]
        (sms,) = frames(got, "sms")
        assert (sms["slot"], sms["src"], sms["dst"], sms["text"]) == (1, 9_990_002, 9_990_001,
                                                                      "Hello from the DMR simulator")
        # While a call went unheard the session said why.
        assert any(i["type"] == "state" and i.get("why") == "needs_codec:mbelib" for i in got)
        st = host.status(sid)
        m = st["meta"]
        assert m["mode"] == "bs" and m["cc"] == 1
        one, two = m["slots"]
        assert (one["slot"], one["state"], one["src"], one["dst"], one["alias"]) == (1, "idle", 9_990_001, 99, "MX0TST Test")
        assert (two["slot"], two["src"], two["encrypted"]) == (2, 9_990_003, True)
        assert st["stats"]["calls"] == 3 and abs(st["stats"]["offsetHz"] - 800) < 30


def test_refusals_are_codes():
    with pytest.raises(ValueError):
        _native.testsig_dmr(mode="trunked")
    with Host(cpu_max=0.0) as host:
        with pytest.raises(DecoderError) as e:
            host.start("dmr", {"slot": "3"}, "iq", RATE)
        assert e.value.args[0] == "bad_params"


@pytest.mark.skipif(not os.environ.get("SHACK_TEST_MBELIB"),
                    reason="set SHACK_TEST_MBELIB to a developer's own libmbe.so.1 (never in the repository) to run this")
def test_with_mbelib_the_voice_is_heard_at_radios_rate(monkeypatch, tmp_path):
    monkeypatch.setenv("SHACK_CODEC_MBELIB", os.environ["SHACK_TEST_MBELIB"])
    monkeypatch.setattr(codecs.Loader, "_system", staticmethod(lambda names: []))
    codecs.configure(tmp_path / "codecs", cache_path=tmp_path / "cache.json")
    st = {s["id"]: s for s in codecs.status()}
    assert st["mbelib"]["state"] == "found", st["mbelib"]
    try:
        e = next(e for e in shack_sdr.catalogue() if e["id"] == "dmr")
        assert e["available"] and not e["partial"]
        x = iq(slots=[[{"kind": "voice", "src": 1, "dst": 2, "superframes": 6, "seed": 5}], []])
        with Host(cpu_max=0.0, ring_s=12.0) as host:
            sid = host.start("dmr", {"slot": "1"}, "iq", RATE)
            feed(host, sid, x)
            got = collect(host, sid, lambda g: frames(g, "call_end"))
            assert frames(got, "call_end") and not any(i.get("why") == "needs_codec:mbelib" for i in got)
            time.sleep(0.2)
            chunks = host.audio(sid, max_s=8.0, rate=12_000, channels=1)
            pcm = np.concatenate([c["pcm"] for c in chunks])
            assert {c["rate"] for c in chunks} == {12_000}
            assert len(pcm) > 12_000 * 2 and np.sqrt(np.mean(pcm ** 2)) > 1e-3
    finally:
        _native.codec_use("mbelib", None)
