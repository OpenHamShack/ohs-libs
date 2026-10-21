"""Each Pluto's crystal correction, kept on this computer (PLAN-0.2 §3.6.3,
D5).

A Pluto's 40 MHz reference is only as good as its crystal: the Tezuka nano
was -19.1 ppm, 24.7 kHz low at 1298.5 MHz, which puts an FM packet signal
most of the way out of a 25 kHz channel. `xo_correction` (on ad9361-phy)
fixes it, but only at run time - the Pluto forgets it at every boot (the
nano's 40000000 -> 39999236 on 18/10/2026). So the value is kept here, per
Pluto (identity.PlutoIdentity.key: serial, else the USB network's MAC), and
the app writes it at every open.

The sign convention, once: `ppm` is the Pluto's own clock error, negative
when it runs slow, and `xo_correction = round(40 MHz x (1 + ppm / 1e6))` -
the nano's -19.1 ppm gives 39 999 236, the value that fixed it. Two ways to
measure it:

- `ppm_from_tx(heard_hz, sent_hz)`: the Pluto's own carrier, sent at
  `sent_hz` with no correction, read at `heard_hz` on a calibrated receiver
  (it lands low when the clock is slow);
- `ppm_from_rx(apparent_hz, true_hz)`: a signal whose frequency is known
  (a beacon, a station on a calibrated radio, an APRS channel's average
  over many frames) that the uncorrected Pluto reports at `apparent_hz`
  (it lands HIGH when the clock is slow: the Pluto's idea of a hertz is
  too long).

The driver only takes values within +-8000 of where it is now
(`xo_correction_available`, both Plutos measured 18/10/2026: [39992000 1
40008000]), i.e. +-200 ppm; anything further is a mistake, not a crystal.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

NOMINAL_HZ = 40_000_000
MAX_PPM = 200.0
FILE = "pluto-xo.json"


#: Why a value was refused, for the app's translations.
REASONS = {"xo-out-of-range": "that is more than a crystal is ever off (200 ppm): measure again"}


class XoError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def correction_for(ppm: float) -> int:
    if abs(ppm) > MAX_PPM:
        raise XoError("xo-out-of-range", f"{ppm:+.1f} ppm")
    return int(round(NOMINAL_HZ * (1.0 + ppm / 1e6)))


def ppm_of(correction: int) -> float:
    return (correction / NOMINAL_HZ - 1.0) * 1e6


def ppm_from_tx(heard_hz: float, sent_hz: float) -> float:
    return (heard_hz - sent_hz) / sent_hz * 1e6


def ppm_from_rx(apparent_hz: float, true_hz: float) -> float:
    return (true_hz / apparent_hz - 1.0) * 1e6


class XoStore:
    """`pluto-xo.json` in the app's data folder: {key: {xo_correction, ppm,
    how, when}}. Written whole through a temporary file, so a crash mid-save
    leaves the old file, never half of one."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / FILE

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str | None) -> int | None:
        if not key:
            return None
        entry = self._load().get(key)
        try:
            value = int(entry["xo_correction"])
        except (TypeError, KeyError, ValueError):
            return None
        return value if abs(ppm_of(value)) <= MAX_PPM else None

    def entry(self, key: str | None) -> dict | None:
        return self._load().get(key) if key else None

    def put(self, key: str, ppm: float, how: str) -> int:
        value = correction_for(ppm)
        data = self._load()
        data[key] = {"xo_correction": value, "ppm": round(ppm, 3), "how": how,
                     "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        return value

    def forget(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, self.path)
