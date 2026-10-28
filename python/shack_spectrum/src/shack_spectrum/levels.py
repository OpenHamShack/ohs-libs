"""The server's quantisation window (CLIENT-UI-PLAN.md §2.3).

One window per stream, chosen by the server so every client quantises the
same way: floor = the noise estimate (20th percentile, the rule every
client already used for itself) minus 10 dB; top = the larger of floor +
60 dB and the 99.7th percentile + 6 dB.

Hysteresis: the window moves only when the wanted one has differed from
it by more than 3 dB at either edge, on every row, for 2 s of capture
time. Moving it invalidates the meaning of every row a client holds, so a
move should be rare (levelsChanged); a passing carrier or a fade does not
move it. "Either edge, either direction" is deliberate: a window that only
ever widened would keep a gone signal's top and waste resolution on it.

This is not the display's auto-levels (§4.2), which the client runs
itself, faster, from the noiseDb/peakDb sent about once a second.

Percentiles are numpy's default (linear between closest ranks, R type 7),
which a Kotlin/Swift/JS port needs to match to reproduce the vectors.
"""

from __future__ import annotations

import numpy as np

NOISE_PCT = 20.0
PEAK_PCT = 99.7
BELOW_NOISE_DB = 10.0
MIN_RANGE_DB = 60.0
ABOVE_PEAK_DB = 6.0
HYSTERESIS_DB = 3.0
HOLD_MS = 2000


def wanted(db) -> tuple[float, float, float, float]:
    """(floor, top, noiseDb, peakDb) for one row, before hysteresis."""
    a = np.asarray(db, dtype=np.float64)
    noise = float(np.percentile(a, NOISE_PCT))
    floor = noise - BELOW_NOISE_DB
    top = max(floor + MIN_RANGE_DB, float(np.percentile(a, PEAK_PCT)) + ABOVE_PEAK_DB)
    return floor, top, noise, float(a.max())


class LevelsWindow:
    """Feed it each source row of a stream; update() says when the window
    moved, which is when the server sends a levels message and sets
    levelsChanged on each client's next row."""

    def __init__(self):
        self.floor: float | None = None
        self.top: float | None = None
        self.noise_db: float | None = None
        self.peak_db: float | None = None
        self._breach_since: int | None = None

    def update(self, db, t_ms: int) -> bool:
        floor, top, self.noise_db, self.peak_db = wanted(db)
        if self.floor is None:
            self.floor, self.top = floor, top
            return True
        if abs(floor - self.floor) <= HYSTERESIS_DB and abs(top - self.top) <= HYSTERESIS_DB:
            self._breach_since = None
            return False
        if self._breach_since is None:
            self._breach_since = t_ms
        if t_ms - self._breach_since < HOLD_MS:
            return False
        # Move to where the signal is now, not where it was when it first
        # left: after 2 s the latest row is the best estimate.
        self.floor, self.top = floor, top
        self._breach_since = None
        return True

    def step(self, depth: int = 8) -> float:
        """dB per value step. The window over 2**depth steps, not
        2**depth - 1, so a 96 dB window is 0.375 dB exactly (the plan's
        example); the top value then reads one step under top, which no
        colour map can show."""
        return (self.top - self.floor) / (1 << depth)

    def message(self, stream: str, depth: int = 8) -> dict:
        """The levels message for one client (its depth sets the step)."""
        return {"type": "levels", "stream": stream, "dbFloor": self.floor, "dbStep": self.step(depth),
                "noiseDb": self.noise_db, "peakDb": self.peak_db}
