"""ADIF 3.1.4 import and export for the logbook (SERVICES.md §3.3).

Stored QSO fields are ADIF names in lower case, so most of the mapping is
"upper-case the name". The exceptions are the four timestamps (`start`
and `end` replace QSO_DATE+TIME_ON and QSO_DATE_OFF+TIME_OFF), the two
frequencies (integer Hz on the wire, MHz with six decimals in ADIF) and
the record's identity (APP_SHACK_UUID, APP_SHACK_ORIGIN), written on every
record so a file exported here and imported elsewhere is the same
contacts, not copies.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# ADIF 3.1.4's Band enumeration, Hz: the logbook's `band` when a QSO has a
# frequency and no band (and the query index's). Same table as
# data/bandplan/vocab.json "bands"; kept here because shack-client is
# stdlib-only and does not depend on shack-bandplan.
BANDS = [
    ("2190m", 135_700, 137_800), ("630m", 472_000, 479_000), ("560m", 501_000, 504_000),
    ("160m", 1_800_000, 2_000_000), ("80m", 3_500_000, 4_000_000), ("60m", 5_060_000, 5_450_000),
    ("40m", 7_000_000, 7_300_000), ("30m", 10_100_000, 10_150_000), ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000), ("15m", 21_000_000, 21_450_000), ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000), ("8m", 40_000_000, 45_000_000), ("6m", 50_000_000, 54_000_000),
    ("5m", 54_000_001, 69_900_000), ("4m", 70_000_000, 71_000_000), ("2m", 144_000_000, 148_000_000),
    ("1.25m", 222_000_000, 225_000_000), ("70cm", 420_000_000, 450_000_000), ("33cm", 902_000_000, 928_000_000),
    ("23cm", 1_240_000_000, 1_300_000_000), ("13cm", 2_300_000_000, 2_450_000_000),
    ("9cm", 3_300_000_000, 3_500_000_000), ("6cm", 5_650_000_000, 5_925_000_000),
    ("3cm", 10_000_000_000, 10_500_000_000), ("1.25cm", 24_000_000_000, 24_250_000_000),
    ("6mm", 47_000_000_000, 47_200_000_000), ("4mm", 75_500_000_000, 81_000_000_000),
    ("2.5mm", 119_980_000_000, 123_000_000_000), ("2mm", 134_000_000_000, 149_000_000_000),
    ("1mm", 241_000_000_000, 250_000_000_000),
]


def band_for(hz) -> str:
    try:
        f = int(hz)
    except (TypeError, ValueError):
        return ""
    for name, lo, hi in BANDS:
        if lo <= f <= hi:
            return name
    return ""


_TAG = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+)(?::[A-Za-z])?)?>")


def parse(text: str) -> list[tuple[int, dict]]:
    """[(line number, {FIELD: value})] for every record of an .adi file.
    Field lengths are honoured, so a value may contain '<' or a newline."""
    body = text
    m = re.search(r"<eoh>", text, re.I)
    start = m.end() if m else (0 if text.lstrip().startswith("<") else len(text))
    out, cur, cur_line = [], {}, None
    i = start
    while True:
        t = _TAG.search(body, i)
        if not t:
            break
        name = t.group(1).upper()
        if name == "EOR":
            if cur:
                out.append((cur_line or 1, cur))
            cur, cur_line = {}, None
            i = t.end()
            continue
        if t.group(2) is None:
            i = t.end()
            continue
        n = int(t.group(2))
        value = body[t.end():t.end() + n]
        if cur_line is None:
            cur_line = body.count("\n", 0, t.start()) + 1
        cur[name] = value
        i = t.end() + n
    return out


def _iso(date: str, time_: str) -> str:
    t = (time_ or "0000").ljust(6, "0")[:6]
    return datetime.strptime(date + t, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_fields(adif: dict) -> tuple[dict, str, str]:
    """An ADIF record as stored fields, plus its APP_SHACK_UUID and
    APP_SHACK_ORIGIN (empty when absent)."""
    a = dict(adif)
    out = {}
    uid = a.pop("APP_SHACK_UUID", "").lower()
    origin = a.pop("APP_SHACK_ORIGIN", "")
    date, on = a.pop("QSO_DATE", ""), a.pop("TIME_ON", "")
    date_off, off = a.pop("QSO_DATE_OFF", ""), a.pop("TIME_OFF", "")
    if date:
        out["start"] = _iso(date, on)
    if off:
        # TIME_OFF without QSO_DATE_OFF ends on the start date
        # (icom/app/logbook.py adif_to_record).
        d = date_off or date
        if d:
            out["end"] = _iso(d, off)
    for src, dst in (("FREQ", "freq_hz"), ("FREQ_RX", "freq_rx_hz")):
        if src in a:
            v = a.pop(src)
            try:
                out[dst] = int(round(float(v) * 1_000_000))
            except ValueError:
                out[dst.replace("_hz", "")] = v
    for k, v in a.items():
        out[k.lower()] = v
    return out, uid, origin


def _field(name: str, value) -> str:
    s = str(value)
    return f"<{name}:{len(s)}>{s} "


def write(records: list[dict], program_version: str = "") -> str:
    """ADIF text for live QSO records (wire shape)."""
    out = ["OpenHamShack logbook export\n", _field("ADIF_VER", "3.1.4"), _field("PROGRAMID", "OpenHamShack")]
    if program_version:
        out.append(_field("PROGRAMVERSION", program_version))
    out.append("<EOH>\n")
    for rec in records:
        f = dict(rec["fields"])
        parts = []
        start = f.pop("start", None)
        if start:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            parts += [_field("QSO_DATE", dt.strftime("%Y%m%d")), _field("TIME_ON", dt.strftime("%H%M%S"))]
        end = f.pop("end", None)
        if end:
            dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            parts += [_field("QSO_DATE_OFF", dt.strftime("%Y%m%d")), _field("TIME_OFF", dt.strftime("%H%M%S"))]
        for src, dst in (("freq_hz", "FREQ"), ("freq_rx_hz", "FREQ_RX")):
            if src in f:
                parts.append(_field(dst, f"{int(f.pop(src)) / 1_000_000:.6f}"))
        for k in sorted(f):
            if f[k] is None or f[k] == "":
                continue
            parts.append(_field(k.upper(), f[k]))
        parts.append(_field("APP_SHACK_UUID", rec["id"]))
        if (rec.get("origin") or {}).get("app"):
            parts.append(_field("APP_SHACK_ORIGIN", rec["origin"]["app"]))
        out.append("".join(parts) + "<EOR>\n")
    return "".join(out)
