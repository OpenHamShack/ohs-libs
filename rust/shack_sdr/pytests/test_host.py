"""shack_sdr's Python face (RADIO-SDR-PLAN §4.2), run for real: the
catalogue, RTTY decoded from numpy IQ and audio through Host, results
numbered by `rev`, refusals as codes, the GIL released while decoding,
a Python-backed CW session beside the Rust ones, and close()."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

import shack_sdr
from shack_sdr import DecoderError, Host, _native

TEXT = "CQ CQ DE GM4BRI GM4BRI K\n"


def rtty(iq: bool, **kw) -> np.ndarray:
    raw = _native.testsig_rtty(TEXT, snr_db=0.0, seed=21, iq=iq, centre=0.0 if iq else 1000.0, **kw)
    return np.frombuffer(raw, np.complex64 if iq else np.float32)


def poll_text(host: Host, sid: str, until: str, secs: float = 10.0) -> tuple[str, list[dict]]:
    items, end = [], time.monotonic() + secs
    while time.monotonic() < end:
        items += host.poll()
        text = "".join(i["text"] for i in items if i["session"] == sid and i["type"] == "text")
        if text.rstrip(" ").endswith(until):
            return text, items
        time.sleep(0.01)
    return "".join(i["text"] for i in items if i["session"] == sid and i["type"] == "text"), items


def feed_all(host, sid, x, block=1200):
    for k in range(0, len(x), block):
        host.feed(sid, x[k:k + block], k / 12_000.0, k)


def test_the_catalogue_lists_every_decoder_once_with_its_engine():
    cat = shack_sdr.catalogue()
    ids = [e["id"] for e in cat]
    assert len(ids) == len(set(ids))
    rtty_entry = next(e for e in cat if e["id"] == "rtty")
    assert rtty_entry["engine"] == "shack_sdr" and rtty_entry["available"] and rtty_entry["fixed"]
    assert rtty_entry["panel"] == "text" and rtty_entry["inputs"] == ["iq"]
    baud = next(p for p in rtty_entry["params"] if p["key"] == "baud")
    assert baud["type"] == "choice" and baud["choices"] == [45.45, 50.0, 75.0] and baud["default"] == 45.45
    shift = next(p for p in rtty_entry["params"] if p["key"] == "shift")
    assert shift["choices"] == [170.0, 425.0, 450.0, 850.0]
    engines = {e["id"]: e["engine"] for e in cat}
    assert engines["cw"] == "shack_cw"
    assert {engines[m] for m in ("ft8", "ft4", "q65", "js8")} == {"ohsdigi"}
    for e in cat:
        assert set(e) >= {"id", "family", "inputs", "rateHz", "bandwidthHz", "outputs", "panel", "params", "needs",
                          "engine", "available", "why", "version", "fixed"}
        assert e["available"] or e["why"] == "not_installed"
    # The test decoders are never offered to an app.
    assert not any(i.startswith("test_") for i in ids)


@pytest.mark.parametrize("iq", [True, False])
def test_rtty_through_the_host_from_numpy(iq):
    with Host(cpu_max=0.0, ring_s=30.0) as host:
        sid = host.start("rtty", {"baud": 45.45, "shift": 170, "reverse": False}, "iq" if iq else "audio", 12_000,
                         center_hz=1000.0)
        feed_all(host, sid, rtty(iq))
        text, items = poll_text(host, sid, "K\n")
        assert text == TEXT
        revs = [i["rev"] for i in items if i["session"] == sid]
        assert revs == list(range(revs[0], revs[0] + len(revs)))   # no gaps
        st = host.status(sid)
        assert st["decoder"] == "rtty" and st["state"] in ("synced", "nosignal")
        assert st["stats"]["fed"] == len(rtty(iq)) and st["stats"]["dropped"] == 0
        assert {"snrDb", "offsetHz", "cpu", "chars"} <= set(st["stats"])


def test_refusals_are_codes():
    with Host() as host:
        with pytest.raises(DecoderError) as e:
            host.start("nonesuch")
        assert e.value.args[0] == "unknown_decoder"
        with pytest.raises(DecoderError) as e:
            host.start("rtty", {"baud": 110})
        assert e.value.args == ("bad_params", "bad_param")
        with pytest.raises(DecoderError) as e:
            host.start("rtty", {"shift": "wide"})
        assert e.value.args[0] == "bad_params"
        sid = host.start("rtty")
        with pytest.raises(DecoderError) as e:
            host.feed(sid, np.zeros(10, np.float32), 0.0, 0)
        assert e.value.args == ("bad_input", "input_kind")
        with pytest.raises(DecoderError) as e:
            host.status("ds_none")
        assert e.value.args[0] == "unknown_session"
        with pytest.raises(DecoderError) as e:
            host.start("ft8")       # a mode decoder only, in M1
        assert e.value.args[0] == "decoder_unavailable"


def test_the_gil_is_released_while_decoding():
    # 40 ms of CPU a block on the decoder's thread; Python keeps counting
    # meanwhile, which it could not if the decoder held the GIL.
    with Host(cpu_max=0.0, testing=True) as host:
        sid = host.start("test_burn", {"ms": 40.0}, "audio", 1000.0)
        for k in range(25):
            host.feed(sid, np.zeros(100, np.float32), 0.0, k * 100)
        count, start = 0, time.monotonic()
        while time.monotonic() - start < 0.5:
            count += 1
        items = host.poll()
        assert len([i for i in items if i["type"] == "text"]) >= 5    # the worker was decoding all along
        assert count > 100_000, count


def test_feed_returns_at_once_and_counts_drops():
    with Host(testing=True) as host:
        sid = host.start("test_sleep", {"ms": 200.0}, "iq", 1000.0)
        t = time.monotonic()
        for k in range(100):
            host.feed(sid, np.zeros(100, np.complex64), 0.0, k * 100)
        assert time.monotonic() - t < 0.2
        assert host.status(sid)["stats"]["dropped"] >= 7000


def test_a_panic_is_one_sessions_error():
    with Host(testing=True) as host:
        bad = host.start("test_panic", {"after": 0}, "audio", 1000.0)
        good = host.start("test_echo", None, "audio", 1000.0)
        for k in range(3):
            host.feed(bad, np.zeros(10, np.float32), 0.0, k * 10)
            host.feed(good, np.zeros(10, np.float32), 0.0, k * 10)
        _, items = poll_text(host, good, "10@20", 3.0)
        states = [i for i in items if i["session"] == bad and i["type"] == "state"]
        assert states[-1]["state"] == "error" and states[-1]["why"] == "panic"
        assert host.status(good)["state"] != "error"


def test_close_joins_promptly():
    host = Host(threads=2, testing=True)
    sid = host.start("test_sleep", {"ms": 150.0}, "audio", 1000.0)
    for k in range(20):
        host.feed(sid, np.zeros(100, np.float32), 0.0, k * 100)
    t = time.monotonic()
    assert host.close()
    assert time.monotonic() - t < 1.0


def test_cw_runs_as_a_python_backed_session_beside_rtty():
    shack_cw = pytest.importorskip("shack_cw")
    from shack_cw.morse import envelope
    rate = 12_000
    env = np.asarray(envelope("VVV CQ TEST", 20.0, rate), dtype=np.float64)
    t = np.arange(len(env)) / rate
    audio = (0.5 * env * np.sin(2 * np.pi * 700.0 * t)).astype(np.float32)
    audio = np.concatenate([np.zeros(rate, np.float32), audio, np.zeros(2 * rate, np.float32)])
    with Host(cpu_max=0.0) as host:
        cw = host.start("cw", {"pitchHz": 700.0}, "audio", rate)
        assert cw in host.sessions()
        # (VVV first: shack_cw learns the speed on it, as from any caller.)
    # Fed at ten times real time: a Python-backed session's queue holds
        # two seconds, as the Rust ring does.
        for k in range(0, len(audio), 1200):
            host.feed(cw, audio[k:k + 1200], k / rate, k)
            time.sleep(0.01)
        text, items = poll_text(host, cw, "TEST")
        assert host.status(cw)["stats"]["dropped"] == 0
        assert text.replace(" ", "").endswith("CQTEST")
        assert all(i["type"] in ("text", "state") for i in items)
        assert host.status(cw)["decoder"] == "cw"
        host.stop(cw)
        assert cw not in host.sessions()
    assert shack_cw.Decoder is not None


def test_nothing_here_transmits():
    """Receive only by construction: no transmit verb anywhere in the
    package's public surface, and no radio link or rig library imported."""
    import sys
    import shack_sdr.sources  # noqa: F401 - the sources too
    names = set()
    for mod in [m for n, m in sys.modules.items() if n.startswith("shack_sdr")]:
        for n in dir(mod):
            obj = getattr(mod, n)
            names.add(n.lower())
            names.update(a.lower() for a in dir(obj) if isinstance(obj, type))
    for verb in ("transmit", "tx_start", "key", "ptt", "keyup", "set_tx"):
        assert verb not in names, verb
    # In a fresh interpreter: importing all of shack_sdr pulls in no radio
    # link, rig library or Packet code.
    import subprocess
    code = "import sys, shack_sdr, shack_sdr.sources; print(chr(10).join(sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert not [m for m in out if m.startswith(("shack_rig", "shack_packet", "serial"))]
