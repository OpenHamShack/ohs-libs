"""Where the station is, from a GPS: NMEA 0183 sentences from a serial
receiver, or gpsd's JSON reports, into one `Fix`.

    reader = NmeaReader()
    for line in serial_lines:
        fix = reader.line(line)       # a Fix when a sentence completed one, else None

Only what APRS needs: position, speed, course, altitude, time, and
whether the receiver says the fix is good. RMC carries position, speed
and course; GGA adds altitude and fix quality; either alone is enough.
`$GP`, `$GN`, `$GL`, `$GA`, `$BD` talkers are all read (a multi-system
receiver says GN). A sentence with a wrong checksum is dropped; one
without a checksum is taken (some cheap receivers leave it off).

gpsd: its "TPV" objects (`{"class":"TPV","mode":3,"lat":...}`) give the
same fields; mode 2 or 3 is a fix. The app connects to gpsd as a client
(localhost:2947, `?WATCH={"enable":true,"json":true}`) - outbound only.
"""
from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass

KNOT_KMH = 1.852


@dataclass
class Fix:
    lat: float
    lon: float
    speed_kmh: float = 0.0
    course_deg: float | None = None
    altitude_m: float | None = None
    time: float | None = None          # UTC seconds, from the receiver
    satellites: int | None = None
    source: str = "nmea"               # nmea | gpsd | manual


def checksum_ok(sentence: str) -> bool | None:
    """True/False for a sentence with a `*HH`, None when it has none."""
    body, star, given = sentence.strip().lstrip("$!").partition("*")
    if not star:
        return None
    total = 0
    for ch in body:
        total ^= ord(ch)
    try:
        return total == int(given[:2], 16)
    except ValueError:
        return False


def _degrees(value: str, hemisphere: str, deg_digits: int) -> float | None:
    if not value or len(value) < deg_digits + 2:
        return None
    try:
        d = int(value[:deg_digits]) + float(value[deg_digits:]) / 60.0
    except ValueError:
        return None
    return -d if hemisphere in ("S", "W") else d


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


class NmeaReader:
    """Keeps the latest RMC and GGA and joins them."""

    def __init__(self):
        self.fix: Fix | None = None
        self._alt: float | None = None
        self._sats: int | None = None
        self._gga_ok = False

    def line(self, text: str) -> Fix | None:
        text = text.strip()
        if not text.startswith("$") or checksum_ok(text) is False:
            return None
        body = text[1:].split("*")[0]
        fields = body.split(",")
        kind = fields[0][2:] if len(fields[0]) == 5 else fields[0]
        if kind == "GGA" and len(fields) >= 10:
            quality = fields[6]
            self._gga_ok = quality not in ("", "0")
            self._alt = _float(fields[9]) if self._gga_ok else None
            self._sats = int(fields[7]) if fields[7].isdigit() else None
            if not self._gga_ok:
                return None
            lat = _degrees(fields[2], fields[3], 2)
            lon = _degrees(fields[4], fields[5], 3)
            if lat is None or lon is None:
                return None
            prev = self.fix
            self.fix = Fix(lat, lon, prev.speed_kmh if prev else 0.0, prev.course_deg if prev else None,
                           self._alt, prev.time if prev else None, self._sats)
            return self.fix
        if kind == "RMC" and len(fields) >= 10:
            if fields[2] != "A":
                self.fix = None
                return None
            lat = _degrees(fields[3], fields[4], 2)
            lon = _degrees(fields[5], fields[6], 3)
            if lat is None or lon is None:
                return None
            knots = _float(fields[7]) or 0.0
            course = _float(fields[8])
            when = None
            hms, dmy = fields[1], fields[9]
            if len(hms) >= 6 and len(dmy) == 6 and dmy.isdigit():
                try:
                    when = float(calendar.timegm((2000 + int(dmy[4:6]), int(dmy[2:4]), int(dmy[0:2]),
                                                  int(hms[0:2]), int(hms[2:4]), int(float(hms[4:])), 0, 0, 0)))
                except ValueError:
                    when = None
            self.fix = Fix(lat, lon, knots * KNOT_KMH, course if knots > 0.5 else None, self._alt, when,
                           self._sats)
            return self.fix
        return None


def from_gpsd(line: str) -> Fix | None:
    """A gpsd JSON line to a Fix, or None (not a TPV, or no fix)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("class") != "TPV" or int(obj.get("mode", 0) or 0) < 2:
        return None
    if "lat" not in obj or "lon" not in obj:
        return None
    speed = float(obj.get("speed") or 0.0) * 3.6           # m/s
    track = obj.get("track")
    when = None
    stamp = obj.get("time")
    if isinstance(stamp, str):
        try:
            when = float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            when = None
    alt = obj.get("altMSL", obj.get("alt"))
    return Fix(float(obj["lat"]), float(obj["lon"]), speed,
               float(track) if track is not None and speed > 1.0 else None,
               float(alt) if alt is not None else None, when, None, "gpsd")


#: What the app sends gpsd after connecting.
GPSD_WATCH = b'?WATCH={"enable":true,"json":true}\n'
