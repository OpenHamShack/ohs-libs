"""Readings from a weather station, for APRS weather reports
(aprs.format_weather). Each reader turns what one kind of station sends
into the parser's names (aprs._WX: wind_dir_deg, wind_kmh, gust_kmh,
temp_c, rain_hour_mm, rain_24h_mm, rain_midnight_mm, humidity_pct,
pressure_hpa); a reading the station does not have is left out.

- **Peet Bros ULTIMETER** in data-logger mode (`!!` and twelve 4-digit
  hex fields, once a second): `peet_line`.
- **Davis Vantage** LOOP packets (99 bytes, "LOO", CRC-CCITT): `davis_loop`;
  the app wakes the console and asks `LOOP 1` (DAVIS_WAKE, DAVIS_LOOP).
- **Anything else, through a file**: a JSON object or a CSV with a header
  row, which WeeWX, Cumulus, a WS-2300 driver or a script can write and
  the app watches: `from_json`, `from_csv`. Units are in the key names
  (`temp_c` or `temp_f`, `wind_kmh`/`wind_mph`/`wind_ms`,
  `pressure_hpa`/`pressure_inhg`, `rain_*_mm`/`rain_*_in`).

The WS-2300 family's own serial protocol (a memory-read dialogue at
2400 baud with a bit-banged reset) is not here: a file from one of the
programs above covers it, and it would be a second protocol to keep
working with no station to test it on.

**[guess]** marks layouts taken from the makers' published notes, not
yet checked against a real station: Peet's field order (from its
"data logging mode" sheet) and the LOOP offsets (Davis' "Vantage Serial
Communication Reference", rev 2.x). The tests check them against
packets written from those documents, which is all that can be done
without the hardware.
"""
from __future__ import annotations

import csv
import io
import json

DAVIS_WAKE = b"\n"
DAVIS_LOOP = b"LOOP 1\n"
DAVIS_ACK = 0x06
LOOP_SIZE = 99


def _crc_ccitt(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        crc &= 0xFFFF
    return crc


def peet_line(line: str) -> dict | None:
    """One ULTIMETER data-logger line (`!!` + 12 x 4 hex digits) **[guess]**:
    wind speed (0.1 km/h), direction (0-255 of a turn), outdoor temperature
    (0.1 °F), long-term rain (0.01 in), pressure (0.1 hPa), indoor
    temperature, outdoor humidity (0.1 %), indoor humidity, date, minutes
    of the day, today's rain (0.01 in), one-minute average wind (0.1 km/h).
    `----` is a missing sensor."""
    line = line.strip()
    if not line.startswith("!!") or len(line) < 2 + 4 * 11:
        return None
    body = line[2:]
    fields = [body[i:i + 4] for i in range(0, len(body) - len(body) % 4, 4)]

    def field(i: int, signed: bool = False) -> int | None:
        if i >= len(fields) or "-" in fields[i]:
            return None
        try:
            v = int(fields[i], 16)
        except ValueError:
            return None
        return v - 0x10000 if signed and v >= 0x8000 else v
    out: dict = {}
    speed, direction, temp = field(0), field(1), field(2, signed=True)
    if speed is not None:
        out["wind_kmh"] = round(speed / 10.0, 1)
    if direction is not None:
        out["wind_dir_deg"] = round((direction & 0xFF) * 360.0 / 256.0)
    if temp is not None:
        out["temp_c"] = round((temp / 10.0 - 32) * 5 / 9, 1)
    pressure, humidity, today = field(4), field(6), field(10)
    if pressure:
        out["pressure_hpa"] = round(pressure / 10.0, 1)
    if humidity is not None:
        out["humidity_pct"] = round(humidity / 10.0)
    if today is not None:
        out["rain_midnight_mm"] = round(today * 0.254, 1)
    avg = field(11)
    if avg is not None and speed is not None:
        out["wind_kmh"] = round(avg / 10.0, 1)
        out["gust_kmh"] = round(max(speed, avg) / 10.0, 1)
    return out


def davis_loop(packet: bytes) -> dict | None:
    """One Vantage LOOP packet **[guess: offsets from the serial reference]**:
    pressure (inHg/1000) at 7, outside temperature (°F/10, signed) at 12,
    wind speed (mph) at 14, its 10-minute average at 15, direction (°) at
    16, outside humidity (%) at 33, rain rate at 41 and day rain at 50
    (both in 0.01 in clicks). None when it is not a LOOP packet or its
    CRC is wrong (a whole packet's CRC, the last two bytes, comes to 0)."""
    if len(packet) < LOOP_SIZE or packet[:3] != b"LOO" or _crc_ccitt(packet[:LOOP_SIZE]) != 0:
        return None
    p = packet

    def u16(at):
        return p[at] | (p[at + 1] << 8)

    def s16(at):
        v = u16(at)
        return v - 0x10000 if v >= 0x8000 else v
    out: dict = {}
    if u16(7):
        out["pressure_hpa"] = round(u16(7) / 1000.0 * 33.8639, 1)
    if s16(12) != 32767:
        out["temp_c"] = round((s16(12) / 10.0 - 32) * 5 / 9, 1)
    if p[14] != 255:
        out["wind_kmh"] = round(p[15 if p[15] != 255 else 14] * 1.609344, 1)
        out["gust_kmh"] = round(max(p[14], p[15] if p[15] != 255 else 0) * 1.609344, 1)
    if u16(16) not in (0, 32767):
        out["wind_dir_deg"] = u16(16) % 360
    if p[33] != 255:
        out["humidity_pct"] = p[33]
    if u16(41) != 65535:
        out["rain_hour_mm"] = round(u16(41) * 0.254, 1)          # the current rate, per hour
    if u16(50) != 65535:
        out["rain_midnight_mm"] = round(u16(50) * 0.254, 1)
    return out


_UNITS = {
    "temp_c": ("temp_c", 1.0, 0.0), "temp_f": ("temp_c", 5 / 9, -32 * 5 / 9),
    "wind_kmh": ("wind_kmh", 1.0, 0.0), "wind_mph": ("wind_kmh", 1.609344, 0.0),
    "wind_ms": ("wind_kmh", 3.6, 0.0), "wind_knots": ("wind_kmh", 1.852, 0.0),
    "gust_kmh": ("gust_kmh", 1.0, 0.0), "gust_mph": ("gust_kmh", 1.609344, 0.0),
    "gust_ms": ("gust_kmh", 3.6, 0.0), "gust_knots": ("gust_kmh", 1.852, 0.0),
    "wind_dir_deg": ("wind_dir_deg", 1.0, 0.0), "wind_dir": ("wind_dir_deg", 1.0, 0.0),
    "humidity_pct": ("humidity_pct", 1.0, 0.0), "humidity": ("humidity_pct", 1.0, 0.0),
    "pressure_hpa": ("pressure_hpa", 1.0, 0.0), "pressure_mbar": ("pressure_hpa", 1.0, 0.0),
    "pressure_inhg": ("pressure_hpa", 33.8639, 0.0),
    "rain_hour_mm": ("rain_hour_mm", 1.0, 0.0), "rain_1h_mm": ("rain_hour_mm", 1.0, 0.0),
    "rain_hour_in": ("rain_hour_mm", 25.4, 0.0), "rain_1h_in": ("rain_hour_mm", 25.4, 0.0),
    "rain_24h_mm": ("rain_24h_mm", 1.0, 0.0), "rain_24h_in": ("rain_24h_mm", 25.4, 0.0),
    "rain_midnight_mm": ("rain_midnight_mm", 1.0, 0.0), "rain_midnight_in": ("rain_midnight_mm", 25.4, 0.0),
    "rain_day_mm": ("rain_midnight_mm", 1.0, 0.0), "rain_day_in": ("rain_midnight_mm", 25.4, 0.0),
}


def from_values(values: dict) -> dict:
    """Named values in any of the units above, to the parser's names. A
    key it does not know, or a value that is not a number, is skipped."""
    out: dict = {}
    for key, value in values.items():
        unit = _UNITS.get(str(key).strip().lower())
        if unit is None or value in (None, ""):
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        name, scale, offset = unit
        out[name] = round(v * scale + offset, 1)
    return out


def from_json(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return from_values(obj) if isinstance(obj, dict) else None


def from_csv(text: str) -> dict | None:
    """A CSV with a header row: the last data row is the reading."""
    rows = list(csv.DictReader(io.StringIO(text)))
    return from_values(rows[-1]) if rows else None


def from_file(text: str) -> dict | None:
    """JSON if it looks like JSON, else CSV."""
    return from_json(text) if text.lstrip().startswith("{") else from_csv(text)
