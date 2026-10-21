"""Finding the keyed signals in the segment, and following them.

From the detection rows (frontend.Stft: 40 ms windows every 20 ms, power):

1. Every PASS_S (0.5 s), over the last HISTORY_S (4 s), per bin: the 90th
   percentile (the key-down level), the 25th (key-up) and the median.
2. The floor: in 2 kHz blocks, a low percentile of the per-bin medians,
   scaled to the noise's mean, and joined up across the segment - the
   floor is not flat across 70 kHz (NB's skimmer does the same, FLOOR_PCT).
3. A candidate is a local maximum of p90 at least `birth_db` over the
   floor, with a valley against any stronger neighbour near it, whose
   power does not simply follow a stronger neighbour's keying (that is the
   neighbour's key clicks, not a station). Its frequency is a parabola
   through three bins of p90 in dB.
4. Tracker: a candidate seen in two passes running is born a track with a
   stable id; tracks are matched to candidates strongest first, within
   MATCH_HZ, and follow them by an average, so a drifting signal is a
   frequency, not a bin. Two tracks that sit within MERGE_HZ for MERGE_S
   are one station (keying sidebands, chirp): the younger folds into the
   older. Unseen for QUIET_S a track is quiet; for FORGET_S it is gone.
5. Carriers: the keying index (p90 over p25, dB) of keyed CW stands well
   over 6 dB, a steady carrier's under 2. A track whose index stays under
   CARRIER_DB for CARRIER_S is a carrier and gets no decoder; one that
   holds within BIRDIE_HZ for BIRDIE_S is a birdie of this receiver, kept
   in a list (Birdies) and never offered a decoder again.

Frequencies here are Hz from the stream's centre except in Birdies, which
holds absolute ones (a birdie belongs to the receiver and the band, and the
stream's centre moves).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PASS_S = 0.5
HISTORY_S = 4.0
FLOOR_BLOCK_HZ = 2000.0
FLOOR_PCT = 20          # of the per-bin medians in a block: the noise bins
VALLEY_DB = 2.0         # a weaker peak must dip this far towards a stronger one
VALLEY_HZ = 200.0       # ... looked for this far round it
CLICK_CORR = 0.8        # power following a stronger neighbour's this closely is its clicks
CLICK_HZ = 400.0

MATCH_HZ = 35.0
MERGE_HZ = 45.0
CLEAR_HZ = 75.0         # no birth this close to a stronger track
MERGE_S = 3.0
QUIET_S = 20.0
FORGET_S = 120.0
CARRIER_DB = 3.0
CARRIER_S = 10.0
BIRDIE_S = 600.0
BIRDIE_HZ = 5.0
FOLLOW = 0.3            # how far a track moves towards its candidate each pass
STEADY_MARGIN_DB = 1.5  # a line keyed no more than this over a steady one's index is steady
COMB_MIN = 4            # carriers on one grid before the grid is believed
COMB_HZ = 3.0           # how close to a grid point a line must sit
COMB_SPACING = (50.0, 2000.0)
SNR_AVG = 0.1           # how far its SNR moves each pass


_STEADY = None


def steady_index(db: float) -> float:
    """The keying index (p90 over p25, dB) an unkeyed line shows at `db`
    (its p90 over the floor) in a detection bin: noise moves even a steady
    line's power, and at a few dB a carrier's index stands far over the
    CARRIER_DB that a strong one shows. Worked out once, by simulation of a
    line in complex Gaussian noise, and interpolated."""
    global _STEADY
    if _STEADY is None:
        rng = np.random.default_rng(1)
        n = rng.standard_normal((40, 4000)) + 1j * rng.standard_normal((40, 4000))
        n /= np.sqrt(2.0)
        amp = np.sqrt(10 ** (np.linspace(-10, 45, 40) / 10))[:, None]
        pw = np.abs(amp + n) ** 2
        p25, p90 = np.percentile(pw, 25, axis=1), np.percentile(pw, 90, axis=1)
        _STEADY = (10 * np.log10(p90), 10 * np.log10(p90 / p25))
    return float(np.interp(db, _STEADY[0], _STEADY[1]))


def comb_of(freqs: list[float]) -> tuple[float, float] | None:
    """(spacing, a line) of a regular comb among steady lines - a switching
    supply's harmonics, ohs-box's 38 lines 240 Hz apart across 20 m - or
    None. The spacing is tried from the gaps between neighbours and their
    halves, thirds and quarters (a comb's lines are not all above the
    floor); believed when COMB_MIN lines sit on it."""
    f = sorted(freqs)
    if len(f) < COMB_MIN:
        return None
    best = None
    for gap in np.diff(f):
        for k in (1, 2, 3, 4):
            sp = gap / k
            if not COMB_SPACING[0] <= sp <= COMB_SPACING[1]:
                continue
            for f0 in f[:3]:
                r = (np.array(f) - f0) / sp
                on = int(np.sum(np.abs(r - np.round(r)) * sp <= COMB_HZ))
                if on >= COMB_MIN and (best is None or on > best[0]):
                    best = (on, float(sp), float(f0))
    return None if best is None else (best[1], best[2])


@dataclass
class Candidate:
    f: float                 # Hz from the stream's centre
    db: float                # p90 over the floor, dB, in a detection bin
    index: float             # keying index, p90 over p25, dB
    snr: float               # key-on SNR in 500 Hz, dB (the RBN's measure)


@dataclass
class Track:
    id: int
    f: float
    born: float
    seen: float
    db: float
    index: float
    snr: float
    hits: int = 1
    carrier: bool = False
    low_since: float | None = None      # index under CARRIER_DB since
    steady_f: float | None = None       # where it has held within BIRDIE_HZ
    steady_since: float | None = None
    near_since: dict = field(default_factory=dict)   # other track id -> since when within MERGE_HZ

    def active(self, t: float) -> bool:
        return t - self.seen < QUIET_S


class Detector:
    """The candidates of each pass, from the detection rows of a segment
    `lo`..`hi` Hz from the stream's centre."""

    def __init__(self, stft, lo: float, hi: float, birth_db: float = 8.0):
        self.stft = stft
        self.birth_db = birth_db
        f = stft.freqs()
        lim = 0.45 * stft.rate
        self.bins = np.nonzero((f >= max(lo, -lim)) & (f <= min(hi, lim)))[0]
        self.bins = self.bins[np.argsort(f[self.bins])]
        self.f = f[self.bins]
        self.rows_per_s = 1.0 / (stft.hop * 4 / stft.rate)
        n = int(round(HISTORY_S * self.rows_per_s))
        self._ring = np.zeros((n, len(self.bins)), np.float32)
        self._filled = 0
        self._at = 0
        self._since = 0
        self._per_pass = max(1, int(round(PASS_S * self.rows_per_s)))
        # Noise in 500 Hz from noise in one detection bin.
        self.to500 = 10 * math.log10(500.0 / stft.det_enbw)
        self.notches: list[tuple[float, float]] = []     # (lo, hi) Hz from centre never offered
        self.floor = np.ones(len(self.bins))

    def add(self, P: np.ndarray) -> list[list[Candidate]]:
        """Take new detection rows; a list of candidate lists, one per pass
        that completed."""
        out = []
        for row in P[:, self.bins] if len(P) else ():
            self._ring[self._at] = row
            self._at = (self._at + 1) % len(self._ring)
            self._filled = min(self._filled + 1, len(self._ring))
            self._since += 1
            if self._since >= self._per_pass and self._filled >= 2 * self._per_pass:
                self._since = 0
                out.append(self.candidates())
        return out

    def _floor(self, med: np.ndarray) -> np.ndarray:
        nb = max(4, int(FLOOR_BLOCK_HZ / self.stft.df))
        k = max(1, len(med) // nb)
        blocks = med[:k * nb].reshape(k, nb)
        centres = np.arange(k) * nb + nb / 2
        # The median of a bin's exponential noise power is ln 2 of its
        # mean, and the floor is the mean: what a signal adds to. (The
        # last part-block, if any, takes its neighbour's level.)
        levels = np.percentile(blocks, FLOOR_PCT, axis=1) / math.log(2)
        return np.interp(np.arange(len(med)), centres, levels)

    def candidates(self) -> list[Candidate]:
        R = self._ring[:self._filled]
        # One partition for all three (np.percentile's interpolation is
        # not needed at 100-200 rows, and its per-call cost was a third of
        # the front end).
        n = len(R)
        ks = [int(0.25 * (n - 1)), int(0.5 * (n - 1)), int(0.9 * (n - 1))]
        p25, med, p90 = np.partition(R, ks, axis=0)[ks].astype(np.float64)
        floor = np.maximum(self._floor(med), 1e-30)
        self.floor = floor
        db = 10 * np.log10(np.maximum(p90, 1e-30) / floor)
        # Local maxima of p90 over the threshold.
        k = np.nonzero((db[1:-1] >= self.birth_db) & (p90[1:-1] >= p90[:-2]) & (p90[1:-1] > p90[2:]))[0] + 1
        if not len(k):
            return []
        order = k[np.argsort(p90[k])[::-1]]
        lp = 10 * np.log10(np.maximum(p90, 1e-30))
        span = int(VALLEY_HZ / self.stft.df)
        cspan = int(CLICK_HZ / self.stft.df)
        kept: list[int] = []
        for i in order:
            ok = True
            for j in kept:
                if abs(i - j) <= span:
                    a, b = sorted((i, j))
                    if lp[a:b + 1].min() > lp[i] - VALLEY_DB:
                        ok = False          # no valley: the stronger one's skirt
                        break
                if abs(i - j) <= cspan and lp[j] - lp[i] > 6.0:
                    x, y = R[:, i], R[:, j]
                    if x.std() > 0 and y.std() > 0 and np.corrcoef(x, y)[0, 1] > CLICK_CORR:
                        ok = False          # keyed with it: its clicks
                        break
            if ok:
                kept.append(int(i))
        out = []
        for i in kept:
            a, b, c = lp[i - 1], lp[i], lp[i + 1]
            den = a - 2 * b + c
            frac = 0.5 * (a - c) / den if den else 0.0
            f = float(self.f[i] + max(-0.5, min(0.5, frac)) * self.stft.df)
            if any(lo <= f <= hi for lo, hi in self.notches):
                continue
            s = max(p90[i] - floor[i], 1e-3 * floor[i]) / floor[i]
            out.append(Candidate(f=f, db=float(db[i]), index=float(10 * np.log10(max(p90[i], 1e-30) / max(p25[i], 1e-30))),
                                 snr=float(10 * math.log10(s) - self.to500)))
        return out

    def level_at(self, f: float) -> float:
        """The floor's level near `f` (for a track's SNR between passes)."""
        return float(np.interp(f, self.f, self.floor))


class Tracker:
    """Tracks from the candidates of successive passes (see the module)."""

    def __init__(self, birdies: "Birdies | None" = None):
        self.comb: tuple[float, float] | None = None    # (spacing, a line), absolute Hz
        self.tracks: dict[int, Track] = {}
        self._pending: list[tuple[Candidate, float]] = []
        self._next = 1
        self.birdies = birdies
        self.centre = 0.0          # the stream's centre, Hz, for the birdie list

    def reset(self) -> None:
        """A new centre or converter: every track starts again (ids go on)."""
        self.tracks.clear()
        self._pending = []

    def update(self, cands: list[Candidate], t: float) -> dict:
        """One pass. Returns {"born": [ids], "merged": [(young, old)],
        "forgotten": [ids], "birdies": [Hz]}."""
        ev = {"born": [], "merged": [], "forgotten": [], "birdies": []}
        cands = sorted(cands, key=lambda c: -c.db)
        free = list(cands)
        # Oldest first, not strongest: on the box's 20 m recording the
        # strongest-first match let a station's keying sidebands, born as
        # tracks of their own beside it, take its candidate pass after pass;
        # the station's own track went quiet, lost its decoder, and was born
        # again with an empty decoder that never learnt the speed.
        tracks = sorted(self.tracks.values(), key=lambda tr: tr.born)
        for tr in tracks:
            best = None
            for c in free:
                if abs(c.f - tr.f) <= MATCH_HZ and (best is None or abs(c.f - tr.f) < abs(best.f - tr.f)):
                    best = c
            if best is None:
                continue
            free.remove(best)
            tr.f += FOLLOW * (best.f - tr.f)
            tr.seen, tr.db, tr.index = t, best.db, best.index
            # Key-on SNR averaged over QSB, as CW Skimmer and the RBN give
            # it (about 5 s of passes): the last pass alone read a fading
            # 20 dB station at 3 dB, in its trough.
            tr.snr += SNR_AVG * (best.snr - tr.snr)
            tr.hits += 1
            self._judge(tr, t, ev)
        # Births: unmatched candidates seen in the last pass too.
        new_pending = []
        live = [tr for tr in self.tracks.values() if tr.active(t)]
        for c in free:
            if self.birdies is not None and self.birdies.near(self.centre + c.f):
                continue
            # Beside a stronger track it is that station's sideband or a
            # second peak of its chirp, not a station (the decoders could not
            # tell two signals this close apart anyway).
            if any(abs(c.f - tr.f) < CLEAR_HZ and tr.db >= c.db for tr in live):
                continue
            prev = next((p for p, _ in self._pending if abs(p.f - c.f) <= MATCH_HZ), None)
            if prev is not None:
                tr = Track(id=self._next, f=0.5 * (prev.f + c.f), born=t, seen=t, db=c.db, index=c.index, snr=c.snr,
                           hits=2)
                # A line on a known comb, no more keyed than a steady line,
                # is the comb's: a carrier from birth, never a decoder.
                if self.on_comb(self.centre + tr.f) and c.index < steady_index(c.db) + 6.0:
                    tr.carrier, tr.low_since = True, t
                self._next += 1
                self.tracks[tr.id] = tr
                ev["born"].append(tr.id)
            else:
                new_pending.append((c, t))
        self._pending = new_pending
        # Merging: two tracks close for MERGE_S are one station.
        live = sorted(self.tracks.values(), key=lambda tr: tr.born)
        gone = set()
        for i, old in enumerate(live):
            if old.id in gone:
                continue
            for young in live[i + 1:]:
                if young.id in gone:
                    continue
                if abs(young.f - old.f) <= MERGE_HZ:
                    since = old.near_since.setdefault(young.id, t)
                    if t - since >= MERGE_S:
                        gone.add(young.id)
                        ev["merged"].append((young.id, old.id))
                else:
                    old.near_since.pop(young.id, None)
        for tid in gone:
            self.tracks.pop(tid, None)
        carriers = [self.centre + tr.f for tr in self.tracks.values() if tr.carrier]
        self.comb = comb_of(carriers) if len(carriers) >= COMB_MIN else self.comb
        # Forgetting.
        for tid, tr in list(self.tracks.items()):
            if t - tr.seen >= FORGET_S:
                del self.tracks[tid]
                ev["forgotten"].append(tid)
        return ev

    def on_comb(self, hz: float) -> bool:
        if self.comb is None:
            return False
        sp, f0 = self.comb
        r = (hz - f0) / sp
        return abs(r - round(r)) * sp <= COMB_HZ

    def _judge(self, tr: Track, t: float, ev: dict) -> None:
        """Carrier and birdie, from the keying index and the frequency. A
        line is steady when its index is no more than a steady line's at
        its level (steady_index) plus STEADY_MARGIN_DB, or under CARRIER_DB:
        a carrier 5 dB over the noise shows an index near 7 dB, keyed CW at
        that level 11 or more."""
        if tr.index < max(CARRIER_DB, steady_index(tr.db) + STEADY_MARGIN_DB):
            if tr.low_since is None:
                tr.low_since = t
            if t - tr.low_since >= CARRIER_S:
                tr.carrier = True
        else:
            tr.low_since = None
            tr.carrier = False
            tr.steady_since = None
        if tr.carrier:
            if tr.steady_f is None or abs(tr.f - tr.steady_f) > BIRDIE_HZ:
                tr.steady_f, tr.steady_since = tr.f, t
            elif tr.steady_since is not None and t - tr.steady_since >= BIRDIE_S and self.birdies is not None:
                f = self.centre + tr.f
                if not self.birdies.near(f):
                    self.birdies.add(f)
                    ev["birdies"].append(f)
        else:
            tr.steady_f = tr.steady_since = None


class Birdies:
    """A receiver's birdies on one band: absolute frequencies never
    offered a decoder again until the list is cleared. Kept as JSON in
    the app's data folder (`path`), or only in memory."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.freqs: list[float] = []
        if self.path and self.path.is_file():
            try:
                self.freqs = [float(x) for x in json.loads(self.path.read_text(encoding="utf-8")).get("hz", [])]
            except (ValueError, OSError, AttributeError):
                self.freqs = []   # a damaged list is an empty one, not a crash

    def near(self, hz: float, within: float = 2 * BIRDIE_HZ) -> bool:
        return any(abs(hz - b) <= within for b in self.freqs)

    def add(self, hz: float) -> None:
        self.freqs.append(round(float(hz), 1))
        self.save()

    def clear(self) -> None:
        self.freqs = []
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"hz": self.freqs}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass   # a read-only data folder costs the list, never the skimmer
