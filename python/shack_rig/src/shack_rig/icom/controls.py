"""The controls an operator turns while a data station runs, and the
meters they watch: RF power, RF gain, squelch; signal, and on transmit
Po, SWR and ALC. For Packet Radio 0.3.0's control strip (Brian,
20/10/2026: "easier to change RX and TX gain/power settings etc").

**Each control is a datasetup.Setting**, so a change goes through
`DataSetup.adjust`: the value as found is saved to the radio's journal
before the first change, every write is read back, and closing the link
puts it back with the rest of the data set-up - the same rule as for
everything else the app changes on a radio, crash-safe the same way.
Levels are the radio's own 0-255 (two BCD bytes, the 14 xx family);
`percent`/`level` convert for a person.

**The meters** are Icom's CI-V Reference Guide's scales (researched
20/10/2026), as the raw value the radio sends (decode_meter: 0000-0255):

| meter | command | scale |
|---|---|---|
| S | 15 02 | civ.smeter_db: 0 S0, 120 S9, 241 S9+60 |
| Po | 15 11 | 0 0 %, 143 50 %, 213 100 % - not linear |
| SWR | 15 12 | civ.swr: 0 1.0, 48 1.5, 80 2.0, 120 3.0 |
| ALC | 15 13 | 0-120 is the ALC zone (0-100 % here); above is over it |

Po above 213 follows the last segment's slope, capped at 120 %: the
guide gives no point there and a precise-looking 150 % would be invented.

`SwrWatch` remembers each transmission's highest SWR and warns when one
is markedly worse than the last: a connector working loose, an antenna
blown round, or a band change onto an antenna that does not suit it.
"""
from __future__ import annotations

from typing import Any, Callable

from ..datasetup import UNAVAILABLE, Setting
from . import civ

RF_GAIN = bytes([0x14, 0x02])
#: (key, the command's two bytes): the controls this module can drive.
CONTROLS = {"rf_power": civ.RF_POWER, "rf_gain": RF_GAIN, "squelch": civ.SQUELCH_LEVEL}
METERS = {"s": b"\x15\x02", "po": b"\x15\x11", "swr": b"\x15\x12", "alc": b"\x15\x13"}
ASK_S = 0.5
PO_POINTS = ((0, 0.0), (143, 50.0), (213, 100.0))
PO_CAP = 120.0
ALC_FULL = 120
#: SwrWatch: a transmission this much worse than the last, and at least
#: SWR_WARN, is worth a word.
SWR_JUMP = 0.5
SWR_WARN = 2.0


def percent(level: int) -> int:
    return round(max(0, min(255, int(level))) * 100 / 255)


def level(pct: float) -> int:
    return max(0, min(255, round(float(pct) * 255 / 100)))


def po_percent(raw: int) -> float:
    raw = max(0, min(255, int(raw)))
    pts = PO_POINTS
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if raw <= x1:
            return round(y0 + (raw - x0) * (y1 - y0) / (x1 - x0), 1)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    return round(min(PO_CAP, y1 + (raw - x1) * (y1 - y0) / (x1 - x0)), 1)


def alc_percent(raw: int) -> float:
    return round(max(0, min(255, int(raw))) * 100 / ALC_FULL, 1)


def _bcd(value: int) -> bytes:
    d = f"{max(0, min(255, int(value))):04d}"
    return bytes([int(d[0:2], 16), int(d[2:4], 16)])


def set_level(command: bytes, value: int) -> bytes:
    return command + _bcd(value)


def settings(rig, call: Callable[..., Any]) -> dict[str, Setting]:
    """The controls as datasetup Settings for `rig` (an IcomRig), keyed by
    CONTROLS' names; `call` runs a function on the rig's loop, as
    icom/datasetup.settings takes it."""
    out = {}
    for key, cmd in CONTROLS.items():
        def read(cmd=cmd):
            got = call(lambda: rig.ask(cmd, cmd, ASK_S, refused=True), timeout=ASK_S + 2)
            if got is None:
                return None
            if got == bytes([civ.NG]):
                return UNAVAILABLE
            return civ.decode_meter(got[2:4]) if len(got) >= 4 else None

        def write(value, cmd=cmd) -> bool:
            return bool(call(rig.send, set_level(cmd, int(value))))
        out[key] = Setting(key, None, read, write, show=lambda v: f"{percent(v)} %")
    return out


def read_meters(rig, call: Callable[..., Any], transmitting: bool) -> dict:
    """The meters now: {"s_db"} receiving; {"po_pct", "swr", "alc_pct"}
    while transmitting. A meter the radio did not answer is None."""
    names = ("po", "swr", "alc") if transmitting else ("s",)
    out: dict = {}
    for name in names:
        cmd = METERS[name]
        got = call(lambda cmd=cmd: rig.ask(cmd, cmd, ASK_S), timeout=ASK_S + 2)
        raw = None
        if got and len(got) >= 4:
            try:
                raw = civ.decode_meter(got[2:4])
            except civ.CivError:
                raw = None
        if name == "s":
            out["s_db"] = None if raw is None else round(civ.smeter_db(raw), 1)
        elif name == "po":
            out["po_pct"] = None if raw is None else po_percent(raw)
        elif name == "swr":
            out["swr"] = None if raw is None else round(civ.swr(raw), 2)
        else:
            out["alc_pct"] = None if raw is None else alc_percent(raw)
    return out


class SwrWatch:
    """The highest SWR of each transmission, and a warning when one is
    markedly worse than the one before (module docstring)."""

    def __init__(self):
        self.last: float | None = None
        self._now: float | None = None

    def seen(self, swr: float | None) -> None:
        if swr is not None:
            self._now = swr if self._now is None else max(self._now, swr)

    def over_ended(self) -> str | None:
        """Call when a transmission ends: a warning, or None."""
        now, self._now = self._now, None
        if now is None:
            return None
        before, self.last = self.last, now
        if before is not None and now >= SWR_WARN and now - before >= SWR_JUMP:
            return f"SWR {now:.1f} on that transmission, up from {before:.1f}: check the antenna and its connectors"
        return None
