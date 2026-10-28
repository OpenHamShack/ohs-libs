"""Capture the golden messages the ADS-B reader and writer are held to
(tests/adsb.rs), from an implementation that is not ours: pyModeS 3
(GPL-3.0, https://github.com/junzis/pyModeS, Junzi Sun - the author of
"The 1090MHz Riddle"), run once on this host and never a dependency of
anything.

    <a venv with pyModeS>/bin/python rust/shack_sdr/tools/adsb_golden.py <pyModeS source tree>

Writes rust/shack_sdr/testdata/adsb/golden.json:

- **heard**: messages real receivers logged - the Riddle's worked examples
  and pyModeS's own corpus of real traffic (`tests/fixtures/golden_v2.json`,
  Amsterdam area, DF17 and Comm-B DF20/21) - with what pyModeS reads from
  each. Our reader must read the same, the address formats' recovered
  address included.
- **pairs**, **local**, **surface**: CPR positions - even/odd pairs from
  the real stream in pyModeS's `tests/data/sample_data_adsb.csv` decoded
  globally, single messages against a reference, and the Riddle's surface
  messages against Schiphol's - as pyModeS places them.
- **stream**: that whole real stream (2000 messages, one receiver, 2016),
  with the station position the test feeds our tracker, so the tracker is
  run on real traffic's real timing.
- **made**: a message of each kind we write, every field set, as our
  writer makes it (testdata/adsb/made.json, from `cargo test --test adsb
  write_made_cases -- --ignored`), with what pyModeS reads from it. Our
  writer must still make those bytes, and pyModeS's reading must equal what
  we asked for: a field one bit out of place, written and read alike by
  us, fails here.

pyModeS's names and units are mapped to ours (tests/adsb.rs `msg_fields`).
pyModeS's code and corpus are GPL-3.0, as this crate is; the messages are
real aircraft's public broadcasts from 2016.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pyModeS as pms
from pyModeS.position import _cpr as cpr

HERE = Path(__file__).resolve().parent.parent / "testdata" / "adsb"
OUT = HERE / "golden.json"
MADE = HERE / "made.json"

RIDDLE = [
    "8D4840D6202CC371C32CE0576098",  # identification: KLM1023
    "8D406B902015A678D4D220AA4BDA",  # identification: EZY85MH (the CRC chapter's)
    "8D40621D58C382D690C8AC2863A7",  # airborne position, even
    "8D40621D58C386435CC412692AD6",  # airborne position, odd
    "8D485020994409940838175B284F",  # velocity, ground speed
    "8DA05F219B06B6AF189400CBC33F",  # velocity, air speed
    "8C4841753AAB238733C8CD4020B1",  # surface position
    "8C4841753A9A153237AEF0F275BE",  # surface position
    "8DA2C1B6E112B600000000760759",  # TC 28: squawk 6513
    "5D484FDEA248F5",                # DF11 all-call
    "20001838CA3E51",                # DF4 altitude
    "28000A00307BCC",                # DF5 squawk
    "A0001838CA3E51F0A8000047A36A",  # DF20 Comm-B
]


def ours(d: dict) -> dict:
    """pyModeS's reading in our names and units (None where it says the
    value is not available)."""
    out = {"df": d["df"], "icao": d["icao"].upper()}
    tc = d.get("typecode")
    if tc is not None:
        out["tc"] = tc
    m = {"altitude": "altitude", "cpr_lat": "cprLat", "cpr_lon": "cprLon", "vertical_rate": "vrate",
         "geo_minus_baro": "geoMinusBaro", "airspeed": "airspeed", "heading": "heading", "squawk": "squawk",
         "emergency_state": "emergency", "flight_status": "fs", "capability": "ca", "version": "version"}
    for k, v in m.items():
        if k in d:
            out[v] = d[k]
    if "callsign" in d:
        out["callsign"] = d["callsign"].rstrip("_ #")
    if tc is not None and 1 <= tc <= 4 and "category" in d:
        out["category"] = (("DCBA"[tc - 1]) + str(d["category"])) if d["category"] else None
    if "cpr_format" in d:
        out["cprOdd"] = bool(d["cpr_format"])
    if "groundspeed" in d:
        out["gs"] = d["groundspeed"]
    if "track" in d:
        if tc is not None and 5 <= tc <= 8 and d.get("track_status") == 0:
            out["track"] = None
        else:
            out["track"] = d["track"]
    if "airspeed_type" in d:
        out["airspeedType"] = d["airspeed_type"].lower()
    if "vr_source" in d:
        out["vrateSource"] = {"GNSS": "geo", "BARO": "baro"}[d["vr_source"]]
    return out


def read(hexmsg: str) -> dict:
    return ours(dict(pms.decode(hexmsg)))


def main() -> None:
    src = Path(sys.argv[1])
    corpus = json.loads((src / "tests" / "fixtures" / "golden_v2.json").read_text())
    heard = [{"hex": h, "pyModeS": read(h)} for h in RIDDLE]
    took = {17: 0, 20: 0, 21: 0}
    for h in corpus:
        df = int(h[:2], 16) >> 3
        if df in took and took[df] < (200 if df == 17 else 40):
            took[df] += 1
            heard.append({"hex": h.upper(), "pyModeS": read(h)})

    stream = []
    with open(src / "tests" / "data" / "sample_data_adsb.csv") as f:
        for row in csv.reader(f):
            stream.append([float(row[0]), row[1].upper()])

    # Even/odd pairs within 10 s from the stream, each aircraft's first few.
    pairs, last, per = [], {}, {}
    for t, h in stream:
        d = dict(pms.decode(h))
        if d.get("df") != 17 or not 9 <= (d.get("typecode") or 0) <= 18 or not d.get("crc_valid"):
            continue
        icao = d["icao"]
        prev = last.get((icao, 1 - d["cpr_format"]))
        last[(icao, d["cpr_format"])] = (t, h, d)
        if prev is None or t - prev[0] > 10 or per.get(icao, 0) >= 10:
            continue
        even, odd = (prev, (t, h, d)) if d["cpr_format"] == 1 else ((t, h, d), prev)
        pos = cpr.airborne_position_pair(even[2]["cpr_lat"], even[2]["cpr_lon"], odd[2]["cpr_lat"],
                                         odd[2]["cpr_lon"], even_is_newer=d["cpr_format"] == 0)
        if pos is None:
            continue
        per[icao] = per.get(icao, 0) + 1
        pairs.append({"even": even[1], "odd": odd[1], "newer": "even" if d["cpr_format"] == 0 else "odd",
                      "lat": pos[0], "lon": pos[1]})

    local = []
    for p in pairs[:20]:
        for k in ("even", "odd"):
            d = dict(pms.decode(p[k], reference=(round(p["lat"] + 0.3, 3), round(p["lon"] - 0.4, 3))))
            local.append({"hex": p[k], "ref": [round(p["lat"] + 0.3, 3), round(p["lon"] - 0.4, 3)],
                          "lat": d["latitude"], "lon": d["longitude"]})

    schiphol = (52.3086, 4.7639)
    surface = []
    for h in ("8C4841753AAB238733C8CD4020B1", "8C4841753A9A153237AEF0F275BE"):
        d = dict(pms.decode(h, surface_ref=schiphol))
        surface.append({"hex": h, "ref": list(schiphol), "lat": d["latitude"], "lon": d["longitude"]})
    e, o = (dict(pms.decode(h)) for h in ("8C4841753AAB238733C8CD4020B1", "8C4841753A9A153237AEF0F275BE"))
    pos = cpr.surface_position_pair(e["cpr_lat"], e["cpr_lon"], o["cpr_lat"], o["cpr_lon"], lat_ref=schiphol[0],
                                    lon_ref=schiphol[1], even_is_newer=False)
    surface_pair = {"even": "8C4841753AAB238733C8CD4020B1", "odd": "8C4841753A9A153237AEF0F275BE",
                    "newer": "odd", "ref": list(schiphol), "lat": pos[0], "lon": pos[1]}

    made = []
    if MADE.exists():
        for c in json.loads(MADE.read_text())["cases"]:
            made.append({**c, "pyModeS": read(c["hex"])})

    doc = {
        "source": f"pyModeS {pms.__version__}; Riddle examples; pyModeS tests/fixtures/golden_v2.json and "
                  "tests/data/sample_data_adsb.csv (GPL-3.0)",
        "heard": heard,
        "pairs": pairs,
        "local": local,
        "surface": surface,
        "surfacePair": surface_pair,
        # The stream was recorded at TU Delft.
        "stream": {"station": [51.9899, 4.3754], "messages": stream},
        "made": made,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=None, separators=(",", ":")) + "\n")
    print(f"{OUT}: {len(heard)} heard, {len(pairs)} pairs, {len(local)} local, {len(made)} made, "
          f"{len(stream)} in the stream")


if __name__ == "__main__":
    main()
