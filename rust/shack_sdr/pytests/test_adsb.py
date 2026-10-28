"""ADS-B through shack_sdr's Python face, run for real: the catalogue entry, a
Host session fed a receiver's own samples with 1090 MHz off its centre, an
aircraft coming back as a frame once it has earned it (a confirmed address,
a position fix seen twice), and the helpers an app's simulator writes its
aircraft with."""

from __future__ import annotations

import time

import numpy as np

import shack_sdr
from shack_sdr import Host, _native


def test_the_catalogue_lists_adsb():
    entry = next(e for e in shack_sdr.catalogue() if e["id"] == "adsb")
    assert entry["inputs"] == ["iq"]
    assert entry["panel"] == "frames"
    assert entry["minRateHz"] == 2_000_000.0
    assert entry["available"]


def squitters(icao: int, lat: float, lon: float, seconds: float) -> list[dict]:
    """Positions twice a second (even, odd), a velocity and a callsign."""
    out = []
    for k in range(int(seconds * 4)):
        t = 0.01 + k * 0.25
        if k % 2 == 0:
            m = {"kind": "airborne", "icao": icao, "alt_ft": 4000, "odd": k % 4 == 2, "lat": lat, "lon": lon}
        elif k % 4 == 1:
            m = {"kind": "velocity", "icao": icao, "east_kn": 0.0, "north_kn": 180.0, "vrate_fpm": -704}
        else:
            m = {"kind": "ident", "icao": icao, "callsign": "TEST12"}
        out.append({"t": t, "payload": _native.adsb_encode(m), "offset_hz": 300_000.0, "phase": 0.1 * k})
    return out


def test_a_session_shows_an_aircraft_at_a_receivers_own_rate():
    rate = 6_000_000.0
    iq = np.frombuffer(_native.testsig_adsb(squitters(0x4CA7B2, 55.55, -4.62, 2.0), rate=rate, duration_s=2.0,
                                            snr_db=20.0, if_bw_hz=5e6), np.complex64)
    host = Host()
    try:
        sid = host.start("adsb", {"refLat": 55.509, "refLon": -4.594}, input="iq", rate=rate)
        block = int(rate / 10)
        for i in range(0, len(iq), block):
            host.feed(sid, iq[i:i + block], 1000.0 + i / rate, i)
        frames, end = [], time.monotonic() + 10
        while not any(f["fields"].get("callsign") for f in frames) and time.monotonic() < end:
            frames += [it for it in host.poll() if it["session"] == sid and it["type"] == "frame"]
            time.sleep(0.01)
        status = host.status(sid)["stats"]
    finally:
        host.close()
    assert frames, status
    f = frames[-1]
    assert f["frame"] == "adsb"
    a = f["fields"]
    assert a["icao"] == "4CA7B2" and a["callsign"] == "TEST12"
    assert abs(a["lat"] - 55.55) < 1e-3 and abs(a["lon"] + 4.62) < 1e-3
    assert a["altBaro"] == 4000 and a["vrate"] == -704 and abs(a["gs"] - 180) < 1
    assert a["onGround"] is False and a["messages"] >= 4
    assert status["aircraft"] == 1 and status["df17"] >= 6


def test_the_helpers_write_and_read():
    b = _native.adsb_encode({"kind": "ident", "icao": 0x4840D6, "callsign": "KLM1023", "ca": 0})
    assert b.hex().upper().startswith("8D4840D6202CC371C32CE0")
    f = _native.adsb_parse(b)
    assert f["df"] == 17 and f["icao"] == "4840D6" and f["callsign"] == "KLM1023" and f["remainder"] == 0
    f = _native.adsb_parse(_native.adsb_encode({"kind": "identity", "icao": 0x4CA7B2, "squawk": "7700"}))
    # An address format's parity is its address: reported, never judged here.
    assert f["df"] == 5 and f["squawk"] == "7700" and f["icao"] == "4CA7B2"
    assert _native.adsb_parse(b"\xff" * 14) is None
