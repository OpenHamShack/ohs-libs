"""Capture the golden messages the AIS reader and writer are held to
(tests/ais.rs), from an implementation that is not ours: pyais (MIT,
https://github.com/M0r13n/pyais), run once on this host and never a
dependency of anything.

    <a venv with pyais>/bin/python rust/shack_sdr/tools/ais_golden.py

Writes rust/shack_sdr/testdata/ais/golden.json, two kinds of case:

- **heard**: sentences real receivers logged, as the AIS literature and
  pyais's documentation quote them (each checked against its own NMEA
  checksum here and again in the test), with what pyais reads from each.
  Our reader must read the same.
- **made**: a message of each type with every field set to something
  other than 0 and its neighbours, written by pyais's own encoder from
  real units. Our writer, given the same values in raw units, must make the
  identical payload bit for bit, and our reader must read them back: a
  field one bit out of place, or two fields swapped, fails here.

pyais's names and units are mapped to ours (src/ais/msg.rs `fields`);
values pyais reports for the standard's "not available" codes are mapped to
null, as ours reports them.
"""

from __future__ import annotations

import enum
import json
from functools import reduce
from pathlib import Path

import pyais
from pyais import decode, encode_dict

OUT = Path(__file__).resolve().parent.parent / "testdata" / "ais" / "golden.json"

HEARD = [
    ["!AIVDM,1,1,,A,15RTgt0PAso;90TKcjM8h6g208CQ,0*4A"],
    ["!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C",
     "!AIVDM,2,2,1,A,88888888880,2*25"],
    ["!AIVDM,1,1,,A,B5NJ;PP005l4ot5Isbl03wsUkP06,0*76"],
    ["!AIVDM,1,1,,A,403OviQuMGCqWrRO9>E6fE700@GO,0*4D"],
    ["!AIVDM,1,1,,B,C5N3SRgPEnJGEBT>NhWAwwo862PaLELTBJ:V00000000S0D:R220,0*0B"],
    ["!AIVDM,1,1,,B,E>kb9O9aS@7PUh10dh19@;0Tah2cWrfP:l?M`00003vP100,0*01"],
    ["!AIVDM,1,1,,A,H52KMeDU653hhhi0000000000000,0*1A"],
    ["!AIVDM,1,1,,B,K815>P8=5EikdUet,0*6B"],
]

# Every field set, in real units for pyais; `raw` is the same in the
# standard's raw units for ours.
MADE = [
    {"name": "type1", "py": {"msg_type": 1, "repeat": 1, "mmsi": 235012345, "status": 7, "turn": 12,
                             "speed": 13.7, "accuracy": 1, "lon": -4.9123, "lat": 55.4321, "course": 271.3,
                             "heading": 270, "second": 41, "maneuver": 1, "raim": 1, "radio": 81234}},
    {"name": "type2", "py": {"msg_type": 2, "repeat": 2, "mmsi": 232004567, "status": 5, "turn": -31,
                             "speed": 0.4, "accuracy": 0, "lon": -5.0625, "lat": 55.6875, "course": 12.6,
                             "heading": 13, "second": 7, "maneuver": 2, "raim": 0, "radio": 3}},
    {"name": "type3", "py": {"msg_type": 3, "repeat": 0, "mmsi": 503123456, "status": 1, "turn": 0,
                             "speed": 102.2, "accuracy": 1, "lon": 151.2093, "lat": -33.8688, "course": 359.9,
                             "heading": 359, "second": 59, "maneuver": 0, "raim": 1, "radio": 524287}},
    {"name": "type4", "py": {"msg_type": 4, "repeat": 0, "mmsi": 2320771, "year": 2026, "month": 10, "day": 28,
                             "hour": 13, "minute": 47, "second": 9, "accuracy": 1, "lon": -4.6123, "lat": 55.8567,
                             "epfd": 7, "raim": 1, "radio": 49153}},
    {"name": "type5", "py": {"msg_type": 5, "repeat": 0, "mmsi": 235087654, "ais_version": 2, "imo": 9387421,
                             "callsign": "MBFL7", "shipname": "ISLE OF ARRAN", "ship_type": 60, "to_bow": 64,
                             "to_stern": 21, "to_port": 8, "to_starboard": 9, "epfd": 1, "month": 10, "day": 29,
                             "hour": 6, "minute": 45, "draught": 3.2, "destination": "BRODICK", "dte": 1}},
    {"name": "type18", "py": {"msg_type": 18, "repeat": 0, "mmsi": 235112233, "speed": 6.4, "accuracy": 1,
                              "lon": -4.8813, "lat": 55.5017, "course": 201.5, "heading": 202, "second": 18,
                              "cs": 1, "display": 0, "dsc": 1, "band": 1, "msg22": 1, "assigned": 0, "raim": 1,
                              "radio": 917510}},
    {"name": "type19", "py": {"msg_type": 19, "repeat": 0, "mmsi": 235998877, "speed": 21.3, "accuracy": 0,
                              "lon": -5.2501, "lat": 55.3002, "course": 45.5, "heading": 44, "second": 31,
                              "shipname": "SEA HAWK", "ship_type": 37, "to_bow": 9, "to_stern": 3, "to_port": 2,
                              "to_starboard": 2, "epfd": 1, "raim": 1, "dte": 0, "assigned": 1}},
    {"name": "type21", "py": {"msg_type": 21, "repeat": 0, "mmsi": 992351234, "aid_type": 20,
                              "name": "LADY ISLE LIGHTHOUS", "accuracy": 1, "lon": -4.7317, "lat": 55.5217,
                              "to_bow": 5, "to_stern": 5, "to_port": 5, "to_starboard": 5, "epfd": 7,
                              "second": 60, "off_position": 0, "raim": 0, "virtual_aid": 0, "assigned": 0}},
    {"name": "type24a", "py": {"msg_type": 24, "repeat": 0, "mmsi": 235445566, "partno": 0, "shipname": "WEE BREEZE"}},
    {"name": "type24b", "py": {"msg_type": 24, "repeat": 0, "mmsi": 235445566, "partno": 1, "ship_type": 36,
                               "vendorid": "SRT", "model": 3, "serial": 654321, "callsign": "2ABC4",
                               "to_bow": 7, "to_stern": 3, "to_port": 1, "to_starboard": 2}},
    {"name": "type24b_aux", "py": {"msg_type": 24, "repeat": 0, "mmsi": 982351234, "partno": 1, "ship_type": 31,
                                   "vendorid": "ABC", "model": 1, "serial": 7, "callsign": "TENDR",
                                   "mothership_mmsi": 235012345}},
    {"name": "type27", "py": {"msg_type": 27, "repeat": 3, "mmsi": 235007001, "accuracy": 1, "raim": 0,
                              "status": 0, "lon": -4.95, "lat": 55.45, "speed": 11, "course": 223, "gnss": 1}},
]

# Ours: raw units for the writer (the standard's).
RAW_SCALE = {"lat": 600_000, "lon": 600_000, "speed": 10, "course": 10, "draught": 10}


def to_raw(py: dict) -> dict:
    raw = {}
    long_range = py["msg_type"] == 27
    for k, v in py.items():
        k2 = {"msg_type": "type", "ship_type": "shiptype"}.get(k, k)
        if long_range and k in ("lat", "lon"):
            v = round(v * 600)
        elif long_range and k in ("speed", "course"):
            v = int(v)
        elif k in RAW_SCALE:
            v = round(v * RAW_SCALE[k])
        if isinstance(v, bool):
            v = int(v)
        raw[k2] = v
    return raw


def plain(v):
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, float) and v.is_integer():
        return v
    return v


# pyais -> ours, and the "not available" codes ours reports as null.
NAMES = {"msg_type": "type", "status": "navStatus", "ship_type": "shipType", "shipname": "shipName",
         "to_bow": "toBow", "to_stern": "toStern", "to_port": "toPort", "to_starboard": "toStarboard",
         "ais_version": "aisVersion", "aid_type": "aidType", "off_position": "offPosition",
         "virtual_aid": "virtualAid", "partno": "partNo", "vendorid": "vendorId",
         "mothership_mmsi": "mothershipMmsi", "gnss": "latency"}
SKIP = {"reserved_1", "reserved_2", "spare_1", "spare_2", "spare_3", "full_name", "name_ext"}


def ours(d: dict) -> dict:
    t = d["msg_type"]
    out = {}
    for k, v in d.items():
        if k in SKIP:
            continue
        v = plain(v)
        k2 = NAMES.get(k, k)
        if t == 5 and k in ("month", "day", "hour", "minute"):
            k2 = "eta" + k.capitalize()
            v = None if (k in ("month", "day") and v == 0) or (k == "hour" and v == 24) or (k == "minute" and v == 60) else v
        if k == "turn":
            k2 = "turnRaw"
        if k == "heading" and v == 511:
            v = None
        if k == "second" and t != 4 and v is not None and v >= 60:
            v = None
        if k == "course" and t != 27 and v == 360:
            v = None
        if k in ("callsign", "shipname", "destination", "vendorid", "name") and v == "":
            v = None
        if k == "imo" and v == 0:
            v = None
        if k == "draught" and v == 0:
            v = None
        out[k2] = v
    if t == 21:
        out["name"] = d.get("full_name") or d.get("name") or None
    return out


def turn_raw(sentences: list[str]) -> int | None:
    bits = "".join(format((c - 48) if c - 48 < 40 else (c - 56), "06b")
                   for c in "".join(s.split(",")[5] for s in sentences).encode())
    if int(bits[:6], 2) not in (1, 2, 3):
        return None
    v = int(bits[42:50], 2)
    return v - 256 if v >= 128 else v


def checksum_ok(s: str) -> bool:
    body, cs = s[1:].split("*")
    return reduce(lambda a, b: a ^ b, body.encode(), 0) == int(cs, 16)


def main() -> None:
    cases = []
    for sentences in HEARD:
        assert all(checksum_ok(s) for s in sentences), sentences
        d = decode(*sentences).asdict()
        want = ours(d)
        if "turnRaw" in want:
            want["turnRaw"] = turn_raw(sentences)
        cases.append({"kind": "heard", "sentences": sentences, "fields": want})
    for m in MADE:
        py = dict(m["py"])
        sentences = encode_dict(py, talker_id="AI", sentence_type="VDM", radio_channel="A")
        d = decode(*sentences).asdict()
        want = ours(d)
        if "turnRaw" in want:
            want["turnRaw"] = turn_raw(sentences)
        raw = to_raw(py)
        if "turn" in raw:
            # pyais takes the rate in degrees a minute and rounds it to the
            # standard's code; ours takes the code itself.
            raw["turn"] = turn_raw(sentences)
        cases.append({"kind": "made", "name": m["name"], "raw": raw, "sentences": sentences,
                      "fields": want})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"pyais": pyais.__version__, "cases": cases}, indent=1) + "\n")
    print(f"{len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
