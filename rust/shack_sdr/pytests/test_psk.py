"""PSK through shack_sdr's Python face, run for real: the catalogue entry,
several stations in one passband decoded by one session from numpy audio,
each character its own result with its time and channel, the channel list
in the session's meta, a channel added live, and refusals as codes."""

from __future__ import annotations

import time

import numpy as np
import pytest

import shack_sdr
from shack_sdr import DecoderError, Host, _native

RATE = 12_000
STATIONS = [(700.0, "CQ CQ de GM4BRI pse k\n", 1), (1206.0, "DL1ABC de G4XYZ 599 tu\n", 2),
            (2140.0, "test de EI2XX qrz?\n", 3)]


def passband(stations, seconds=12.0, noise=0.01) -> np.ndarray:
    """Real audio with several PSK31 stations in it, as a receiver's
    passband carries them."""
    x = np.zeros(int(seconds * RATE), np.float32)
    for f, text, seed in stations:
        s = np.frombuffer(_native.testsig_psk(text, centre=f, seed=seed), np.float32)
        x[:len(s)] += s[:len(x)]
    return x + np.random.default_rng(7).normal(0, noise, len(x)).astype(np.float32)


def collect(host: Host, sid: str, want: int, secs: float = 10.0) -> list[dict]:
    """Every result of the session until `want` characters have come."""
    items, end = [], time.monotonic() + secs
    while time.monotonic() < end:
        items += [i for i in host.poll() if i["session"] == sid]
        if sum(1 for i in items if i["type"] == "text") >= want:
            break
        time.sleep(0.01)
    return items


def by_channel(items: list[dict], offsets: list[float]) -> list[str]:
    out = [""] * len(offsets)
    for i in items:
        if i["type"] == "text":
            k = min(range(len(offsets)), key=lambda k: abs(offsets[k] - i["offsetHz"]))
            out[k] += i["text"]
    return out


def test_the_catalogue_entry():
    e = next(e for e in shack_sdr.catalogue() if e["id"] == "psk")
    assert e["engine"] == "shack_sdr" and e["available"] and e["fixed"] and e["family"] == "amateur"
    assert e["inputs"] == ["iq"] and e["outputs"] == ["text", "meta"] and e["panel"] == "text"
    params = {p["key"]: p for p in e["params"]}
    assert params["baud"]["choices"] == [31.25, 62.5, 125.0] and params["baud"]["default"] == 31.25
    assert params["channels"]["type"] == "text" and params["channels"]["default"] == "0"
    assert params["afcRangeHz"]["default"] == 15.0 and params["squelch"]["default"] == 3.0


def test_several_stations_in_one_passband_from_numpy_audio():
    offsets = [f - 1500.0 for f, _, _ in STATIONS]
    with Host(cpu_max=0.0, ring_s=30.0) as host:
        sid = host.start("psk", {"baud": 31.25, "channels": ",".join(f"{o:g}" for o in offsets)}, "audio", RATE,
                         center_hz=1500.0)
        x = passband(STATIONS)
        for k in range(0, len(x), 1200):
            host.feed(sid, x[k:k + 1200], 1_000.0 + k / RATE, k)
        want = sum(len(_native.psk_truth(t)) for _, t, _ in STATIONS)
        items = collect(host, sid, want)
        assert by_channel(items, offsets) == [_native.psk_truth(t) for _, t, _ in STATIONS]
        texts = [i for i in items if i["type"] == "text"]
        # One result a character, each with its own time (from the samples:
        # the feed started at t = 1000 s), its channel and its SNR.
        assert all(len(i["text"]) == 1 for i in texts)
        assert all(1_001.0 < i["t"] < 1_012.0 and i["snrDb"] is not None for i in texts)
        for o in offsets:
            ts = [i["t"] for i in texts if abs(i["offsetHz"] - o) < 20]
            assert ts == sorted(ts)
        st = host.status(sid)
        assert st["decoder"] == "psk" and st["stats"]["channels"] == 3 and st["stats"]["chars"] == want
        chans = st["meta"]["channels"]
        assert [c["offsetHz"] for c in chans] == offsets
        assert all(abs(c["freqHz"] - c["offsetHz"]) < 15 for c in chans)
        assert abs(chans[1]["freqHz"] - (1206.0 - 1500.0)) < 1.0


def test_a_channel_added_live():
    stations = [(1000.0, "CQ CQ de GM4BRI GM4BRI GM4BRI pse k\n", 1), (1600.0, "DL1ABC de G4XYZ 599 tu\n", 2)]
    with Host(cpu_max=0.0, ring_s=30.0) as host:
        sid = host.start("psk", {"channels": "-300"}, "audio", RATE, center_hz=1300.0)
        x = passband(stations, 16.0)
        for k in range(0, len(x), 1200):
            if k == 6000:
                host.configure(sid, {"channels": "-300,300"})
            host.feed(sid, x[k:k + 1200], k / RATE, k)
        want = sum(len(_native.psk_truth(t)) for _, t, _ in stations)
        items = collect(host, sid, want)
        assert by_channel(items, [-300.0, 300.0]) == [_native.psk_truth(t) for _, t, _ in stations]


def test_refusals_are_codes():
    with Host() as host:
        for bad in ({"channels": "300,abc"}, {"channels": ""}, {"baud": 45.45}, {"afcRangeHz": 500}):
            with pytest.raises(DecoderError) as e:
                host.start("psk", bad, "audio", RATE, center_hz=1500.0)
            assert e.value.args[0] == "bad_params", bad
        sid = host.start("psk", {"channels": "0"}, "audio", RATE, center_hz=1500.0)
        with pytest.raises(DecoderError) as e:
            host.configure(sid, {"channels": "1,1"})
        assert e.value.args[0] == "bad_params"
        # A channel the session's band cannot hold is the decoder's refusal,
        # once it knows the rate: the session goes to error with the code.
        sid = host.start("psk", {"channels": "5900"}, "iq", RATE)
        host.feed(sid, np.zeros(1200, np.complex64), 0.0, 0)
        end = time.monotonic() + 5
        while host.status(sid)["state"] != "error" and time.monotonic() < end:
            time.sleep(0.01)
        assert (host.status(sid)["state"], host.status(sid)["why"]) == ("error", "channel_outside")


def test_an_over_that_stops_on_its_last_character():
    # Radio's drain: the transmitter unkeys on the last character's "00",
    # no idle after it, and the noise goes on. The "k" prints, and before a
    # second of that noise has been fed (psk.py printed it on its "00"; this
    # decoder used to want 0.13 s of idle after it, and a second more).
    text = "CQ DE GM4BRI PSE K"
    x = np.frombuffer(_native.testsig_psk(text, centre=1000.0, snr_db=0.0, postamble_s=0.0, tail_s=3.0, seed=4),
                      np.float32)
    with Host(cpu_max=0.0, ring_s=30.0) as host:
        sid = host.start("psk", {}, "audio", RATE, center_hz=1000.0)
        # Up to 0.9 s past where the carrier stops (three seconds of noise
        # follow it), and no further.
        stop = len(x) - int(3.0 * RATE) + int(0.9 * RATE)
        for k in range(0, stop, 1200):
            host.feed(sid, x[k:min(k + 1200, stop)], k / RATE, k)
        items = collect(host, sid, len(_native.psk_truth(text)), secs=5.0)
        got = "".join(i["text"] for i in items if i["type"] == "text")
        assert got == _native.psk_truth(text)


def test_a_frequency_step_mid_over():
    # NB's own echo: the TX lock moves the sender 6 Hz in the middle of the
    # over, phase-continuously. Nothing is lost, and the channel's tracked
    # frequency is where the carrier went.
    text = "CQ CQ de GM4BRI GM4BRI GM4BRI pse k\n"
    x = np.frombuffer(_native.testsig_psk(text, centre=0.0, snr_db=5.0, seed=50, iq=True), np.complex64).copy()
    at = int(4.7 * RATE)
    x[at:] *= np.exp(2j * np.pi * 6.0 * np.arange(len(x) - at) / RATE).astype(np.complex64)
    with Host(cpu_max=0.0, ring_s=30.0) as host:
        sid = host.start("psk", {}, "iq", RATE)
        for k in range(0, len(x), 1200):
            host.feed(sid, x[k:k + 1200], k / RATE, k)
        items = collect(host, sid, len(_native.psk_truth(text)), secs=5.0)
        got = "".join(i["text"] for i in items if i["type"] == "text")
        assert got == _native.psk_truth(text)
        after = [i["offsetHz"] for i in items if i["type"] == "text" and i["t"] > 5.5]
        assert after and all(abs(f - 6.0) < 0.5 for f in after), after
        assert host.status(sid)["stats"]["replays"] >= 1
