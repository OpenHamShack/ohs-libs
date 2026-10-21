"""The path test: which modes a path carries, one direction at a time.

Asked for 18/10/2026 for Brian and Ray's 23.5 km test: each station has
one antenna and no T/R relay yet, so the cable is moved by hand - the
sender's on its TX port, the receiver's on its RX port - and each
direction is tested on its own, then they swap. The app's Path test page
is built on this module; nothing here keys anything or draws anything.

**Sender** (`plan`, `frame_info`): for each mode chosen, N numbered UI
frames (default 50, a second apart), each saying the run, the mode and
its number - `OHSPT <run> <mode> <seq>/<count>` and padding - sent at that
mode through an adaptive link (Engine.send_ui(..., mode=)), under the
usual arming, guard, caps and band-plan refusal (the app's, as for any
transmission).

**Receiver** (`Tally`): listens on every candidate mode at once (an
adaptive link with Engine.listen_also(ladder), as the adaptive receiver
does) and counts per run and mode: frames decoded and missing, the level
(C/N0) and frequency offset they were heard at, and the receivers' bad-FCS
counts. The mode written in a frame is what it is counted under, not the
receiver that decoded it. `rows()` is the live table, `report()` a short
text report to save, copy or send with diagnostics.

**Bad FCS** counts every frame-shaped run of bits whose check failed,
including ones the framer finds in plain noise between transmissions, so
it is only comparable between modes heard over the same stretch; a frame
lost in a transmission shows as missing.

**Into adaptation**: `best_mode()` is the fastest mode that came through
well enough (90 % by default); the receiving station's
LinkConfig.adapt_start, so its first request for that direction is what
the path carries rather than the top.

**Drive level** (`drive_steps`): an amplifier with no PTT and a drive cap
per band (radios/pluto.py `max_tx_gain_db`) is set up by stepping the
Pluto's gain up from -30 dB, a few dB a short burst, with the operator
watching a power meter or a receiver and stopping at compression or the
target power; the last good step is the cap the app saves for that band.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from . import adapt, modem

TAG = "OHSPT"
COUNT = 50
GAP_S = 1.0
PAD = 100
#: The share of a mode's frames that must arrive for best_mode to pick it.
GOOD_PCT = 90.0


def frame_info(run: str, mode: str, seq: int, count: int, pad: int = PAD) -> str:
    return f"{TAG} {run} {mode} {seq}/{count} " + "x" * pad


def plan(modes, count: int = COUNT, pad: int = PAD):
    """(mode, seq, info) for every frame of a run, mode by mode, slowest
    last: the order a sender transmits them."""
    run = time.strftime("%H%M%S")
    for m in modes:
        name = modem.mode(m).name
        for seq in range(1, count + 1):
            yield name, seq, frame_info(run, name, seq, count, pad)


def parse(info: bytes | str) -> tuple[str, str, int, int] | None:
    """(run, mode, seq, count) from a path test frame's text, or None."""
    text = info.decode("latin-1", "replace") if isinstance(info, (bytes, bytearray)) else str(info)
    words = text.split()
    if len(words) < 4 or words[0] != TAG or "/" not in words[3]:
        return None
    try:
        seq, count = (int(x) for x in words[3].split("/", 1))
        modem.mode(words[2])
    except (ValueError, Exception):                 # noqa: BLE001 - not one of ours
        return None
    return words[1], words[2], seq, count


@dataclass
class _Cell:
    count: int
    seen: set = field(default_factory=set)
    cn0: list = field(default_factory=list)
    offset_hz: list = field(default_factory=list)
    first: float = 0.0
    last: float = 0.0


class Tally:
    """The receiving end's counts (module docstring)."""

    def __init__(self):
        self.cells: dict[tuple[str, str], _Cell] = {}
        self.bad_fcs: dict[str, int] = {}
        self.source: dict[str, str] = {}
        self.expected: dict[str, int] = {}

    def expect(self, modes, count: int = COUNT) -> None:
        """The modes and count the sender said it would send: a mode of
        which nothing at all was heard still gets its row (0 of count)."""
        self.expected = {modem.mode(m).name: count for m in modes}

    def heard(self, frame, cn0: float | None = None, offset_hz: float | None = None,
              now: float | None = None) -> bool:
        """A frame heard (an ax25.Frame); True if it was a path test's."""
        got = parse(frame.info or b"")
        if got is None:
            return False
        run, mode, seq, count = got
        now = time.time() if now is None else now
        cell = self.cells.setdefault((run, mode), _Cell(count, first=now))
        cell.seen.add(seq)
        cell.last = now
        if cn0 is not None:
            cell.cn0.append(cn0)
        if offset_hz is not None:
            cell.offset_hz.append(offset_hz)
        self.source[run] = str(frame.source)
        return True

    def set_bad_fcs(self, by_mode: dict[str, int]) -> None:
        self.bad_fcs = dict(by_mode)

    def rows(self, run: str | None = None) -> list[dict]:
        """One row per mode of a run (the latest by default), fastest first."""
        runs = sorted({r for r, _m in self.cells})
        if not runs:
            return []
        run = run or runs[-1]
        out = []
        cells = {mode: c for (r, mode), c in self.cells.items() if r == run}
        for mode, count in self.expected.items():
            cells.setdefault(mode, _Cell(count))
        for mode, c in cells.items():
            r = run
            got = len(c.seen)
            out.append({"run": r, "mode": mode, "bitrate": modem.mode(mode).bitrate, "decoded": got,
                        "missing": c.count - got, "count": c.count, "pct": round(100.0 * got / c.count, 1),
                        "cn0_dbhz": round(statistics.median(c.cn0), 1) if c.cn0 else None,
                        "offset_hz": round(statistics.median(c.offset_hz)) if c.offset_hz else None,
                        "bad_fcs": self.bad_fcs.get(mode), "from": self.source.get(r)})
        return sorted(out, key=lambda row: -adapt.threshold_dbhz(row["mode"]))

    def best_mode(self, run: str | None = None, good_pct: float = GOOD_PCT) -> str | None:
        """The fastest mode of the run with at least `good_pct` through."""
        good = [row for row in self.rows(run) if row["pct"] >= good_pct]
        return max(good, key=lambda row: row["bitrate"])["mode"] if good else None

    def report(self, run: str | None = None, *, frequency_hz: float | None = None, station: str = "") -> str:
        rows = self.rows(run)
        if not rows:
            return "Path test: nothing heard yet."
        head = f"Path test run {rows[0]['run']} from {rows[0]['from']}"
        head += f" heard by {station}" if station else ""
        head += f" on {frequency_hz / 1e6:.4f} MHz" if frequency_hz else ""
        lines = [head, f"{'mode':>11} {'kbit/s':>7} {'heard':>9} {'%':>6} {'C/N0':>7} {'offset':>8} {'bad FCS':>8}"]
        for r in rows:
            lines.append(f"{r['mode']:>11} {r['bitrate'] / 1e3:>7.1f} {r['decoded']:>4}/{r['count']:<4} "
                         f"{r['pct']:>6.1f} {_fmt(r['cn0_dbhz'], '.1f'):>7} {_fmt(r['offset_hz'], 'd'):>8} "
                         f"{_fmt(r['bad_fcs'], 'd'):>8}")
        best = self.best_mode(run)
        lines.append(f"best mode this way: {best or 'none'}")
        return "\n".join(lines)


def _fmt(v, spec: str) -> str:
    return "-" if v is None else format(v, spec)


def drive_steps(start_db: float = -30.0, step_db: float = 2.0, stop_db: float = 0.0,
                cap_db: float | None = None) -> list[float]:
    """The gains a drive-level helper steps through (module docstring):
    from `start_db` up by `step_db` to `stop_db`, never above `cap_db`."""
    if not 0.5 <= step_db <= 10.0:
        raise ValueError("a step of 0.5-10 dB")
    top = stop_db if cap_db is None else min(stop_db, cap_db)
    n = int(math.floor((top - start_db) / step_db + 1e-9))
    return [round(start_db + i * step_db, 2) for i in range(n + 1)]
