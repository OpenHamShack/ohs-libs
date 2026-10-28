"""AIS through shack_sdr's Python face, run for real: the catalogue entry, a
Host session fed a receiver's own 2.4 MS/s with 162.000 MHz off its centre
(as Radio's AIS session feeds it), both channels' ships coming back as
frames with their fields and sentences, and the helpers an app's simulator
writes its ships with."""

from __future__ import annotations

import time

import numpy as np

import shack_sdr
from shack_sdr import Host, _native


def ship(mmsi: int, lat: float, lon: float, name: str | None = None) -> dict:
    if name is not None:
        return {"type": 5, "mmsi": mmsi, "shipname": name, "callsign": "MABC1", "shiptype": 70,
                "to_bow": 100, "to_stern": 20, "to_port": 10, "to_starboard": 10, "destination": "GREENOCK"}
    return {"type": 1, "mmsi": mmsi, "lat": round(lat * 600_000), "lon": round(lon * 600_000),
            "speed": 105, "course": 1800, "heading": 180, "second": 12, "turn": -128}


def test_the_catalogue_lists_ais():
    entry = next(e for e in shack_sdr.catalogue() if e["id"] == "ais")
    assert entry["inputs"] == ["iq"]
    assert entry["panel"] == "frames"
    assert entry["minRateHz"] == 96_000.0
    assert entry["available"]


def test_a_session_hears_both_channels_at_a_receivers_own_rate():
    rate, centre = 2_400_000.0, 250_000.0
    bursts = [{"t": 0.05, "channel": "A", "fields": ship(235000101, 55.45, -4.80)},
              {"t": 0.12, "channel": "B", "fields": ship(235000102, 55.50, -4.95, name="CLYDE TRADER")},
              {"t": 0.25, "channel": "B", "payload": _native.ais_encode(ship(235000103, 55.40, -4.70)),
               "level_db": -6.0, "offset_hz": 400.0}]
    iq = np.frombuffer(_native.testsig_ais(bursts, rate=rate, duration_s=0.4, centre_hz=centre, snr_db=25.0),
                       np.complex64)
    host = Host()
    try:
        sid = host.start("ais", {"offsetHz": centre}, input="iq", rate=rate)
        block = int(rate / 10)
        for i in range(0, len(iq), block):
            host.feed(sid, iq[i:i + block], 1000.0 + i / rate, i)
        frames, end = [], time.monotonic() + 10
        while len(frames) < 3 and time.monotonic() < end:
            frames += [it for it in host.poll() if it["session"] == sid and it["type"] == "frame"]
            time.sleep(0.01)
    finally:
        host.close()
    got = {f["fields"]["mmsi"]: f for f in frames}
    assert sorted(got) == [235000101, 235000102, 235000103]
    a = got[235000101]
    assert a["frame"] == "ais" and a["fields"]["channel"] == "A"
    assert abs(a["fields"]["lat"] - 55.45) < 1e-5 and abs(a["fields"]["lon"] + 4.80) < 1e-5
    assert a["fields"]["speed"] == 10.5 and a["fields"]["course"] == 180.0
    assert a["fields"]["nmea"][0].startswith("!AIVDM,1,1,,A,")
    b = got[235000102]
    assert b["fields"]["shipName"] == "CLYDE TRADER" and b["fields"]["destination"] == "GREENOCK"
    assert len(b["fields"]["nmea"]) == 2
    c = got[235000103]["fields"]
    assert abs(c["offsetHz"] - 400) < 150 and c["snrDb"] < got[235000101]["fields"]["snrDb"]


def test_the_helpers_write_read_and_armour():
    payload = _native.ais_encode(ship(235000104, 55.5, -4.9))
    assert len(payload) == 21
    f = _native.ais_parse(payload)
    assert f["mmsi"] == 235000104 and f["type"] == 1 and f["heading"] == 180.0
    assert _native.ais_parse(b"\x00" * 21) is None
    s = _native.ais_nmea(payload, "B")
    assert len(s) == 1 and s[0].startswith("!AIVDM,1,1,,B,")
