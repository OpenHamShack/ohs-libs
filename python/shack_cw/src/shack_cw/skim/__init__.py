"""shack_cw.skim: the CW skimmer - every keyed signal in a band's CW
segment, decoded at once, receive only (CW-SKIMMER-PLAN, openhamshack docs).

    sk = Skimmer(96_000, lo=14_000_000, hi=14_070_000, max_channels=64,
                 country=lookup, band="20m")
    for block in segment_blocks:                 # complex64 at 96 kS/s
        spots = sk.feed(block, t=utc, centre=14_035_000)   # cwspot items
    sk.snapshot(utc)                             # a cwskim item

The app hands it one complex stream - its filter bank's channel over the
segment - and gets back the `cwskim` snapshot and the `cwspot` items the
contract describes (openhamshack-nb docs/ENGINE-API.md). Inside: frontend (one
STFT over the segment), detect (peaks, tracks, carriers, birdies), pool
(a Decoder per signal through feed_frames), calls (callsigns and spots);
runner puts all of it in a child process so its Python never holds the
receive thread's GIL; synth renders test bands; bench measures.

Nothing in this package can key a transmitter: it imports numpy and
shack_cw only, and its tests check that by importing it.
"""
from __future__ import annotations

import time as _time

from .calls import Calls
from .detect import Birdies, Detector, Tracker
from .frontend import Stft
from .pool import ENGINE_SETS, Pool

TEXT_SHOWN = 48          # characters of a channel's text in a snapshot
QUIET_FLUSH_S = 4.0      # a channel this long without a character has finished its over
# A signal is followed once its key-down stands this far over the decoders'
# squelch in the detection spectrum. The plan said 2 dB; on ohs-box's 20 m
# recording that bred 420 tracks in 5 minutes, nearly all noise, for the
# same spots that 4 dB found with 160 (and a fifth less CPU).
BIRTH_OVER_DB = 4.0

__all__ = ["Skimmer", "Calls", "Birdies"]


class Skimmer:
    """The whole skimmer on one stream (see the package docstring).

    `lo`/`hi`: the segment, absolute Hz. `centre`: the stream's centre
    (feed() may move it; a move restarts the tracks, as does a new
    `generation`). `country(call)` and `history(call)`: see calls.Calls.
    `birdies`: a detect.Birdies (the app keeps one per device and band).
    `dc`: the receiver's own centre, absolute Hz; a +-500 Hz notch round it
    is never offered a decoder (10 m, the HL2)."""

    # "classic", "nn", or "both": both engines on every channel, each
    # printing its own words for the callsign check (pool.py says why).
    ENGINES = tuple(ENGINE_SETS)

    def __init__(self, rate: float, lo: float, hi: float, centre: float | None = None, *,
                 max_channels: int = 64, squelch_db: float = 6.0, country=None, history=None,
                 birdies: Birdies | None = None, band: str | None = None, pitch: float = 700.0,
                 dc: float | None = None, engine: str = "both"):
        self.rate = float(rate)
        # Which CW engines decode the channels (ENGINES).
        self.engine = engine
        self.lo, self.hi = float(lo), float(hi)
        self.centre = float(centre if centre is not None else 0.5 * (lo + hi))
        self.band = band
        self.squelch_db = float(squelch_db)
        self.max_channels = int(max_channels)
        self.dc = dc
        self.stft = Stft(self.rate)
        self.birdies = birdies or Birdies()
        self.calls = Calls(country=country, history=history, pitch=pitch)
        self.generation = None
        self._t = None                 # the clock: seconds at the end of the last block
        self._samples = 0
        self._build()

    # ----------------------------------------------------------------- set up
    def _build(self) -> None:
        self.stft.reset()
        self.det = Detector(self.stft, self.lo - self.centre, self.hi - self.centre,
                            birth_db=self.squelch_db + BIRTH_OVER_DB)
        if self.dc is not None:
            self.det.notches.append((self.dc - self.centre - 500.0, self.dc - self.centre + 500.0))
        if getattr(self, "tracker", None) is None:
            self.tracker = Tracker(self.birdies)
            self.pool = Pool(self.stft, self.max_channels, self.squelch_db, self.engine)
        else:
            for tid in list(self.tracker.tracks):
                self._forget(tid)
            self.tracker.reset()
            self.pool.reset()
        self.tracker.centre = self.centre
        # Per stream, (track id, engine): each engine's words are its own,
        # judged apart, so one engine's letters never complete the
        # other's call.
        self._last_char: dict[tuple, float] = {}
        self._word: dict[tuple, str] = {}

    def configure(self, *, max_channels: int | None = None, squelch_db: float | None = None) -> None:
        """New settings while running (cwSkimMax, cwSkimSquelch)."""
        if max_channels is not None:
            self.max_channels = self.pool.max_channels = int(max_channels)
        if squelch_db is not None:
            self.squelch_db = self.pool.squelch_db = float(squelch_db)
            self.det.birth_db = self.squelch_db + BIRTH_OVER_DB

    def retune(self, centre: float | None = None, lo: float | None = None, hi: float | None = None,
               band: str | None = None, dc: float | None = None) -> None:
        """A new stream centre or segment: the tracks start again. Spots
        are kept for their 30 minutes, labelled with their band."""
        if centre is not None:
            self.centre = float(centre)
        if lo is not None:
            self.lo = float(lo)
        if hi is not None:
            self.hi = float(hi)
        if band is not None:
            self.band = band
        self.dc = dc if dc is not None else self.dc
        self._build()

    def _forget(self, tid: int) -> None:
        self.pool.drop(tid, forget=True)
        self.calls.forget(tid)
        for key in [k for k in self._last_char if k[0] == tid]:
            self._last_char.pop(key, None)
        for key in [k for k in self._word if k[0] == tid]:
            self._word.pop(key, None)

    # ------------------------------------------------------------------ feed
    def feed(self, z, t: float | None = None, centre: float | None = None, generation=None) -> list[dict]:
        """One block of the segment's complex stream. `t`: seconds (UTC) of
        its first sample, or None to count samples. Returns the cwspot
        `spot` items it made (each with `rev`)."""
        if (centre is not None and float(centre) != self.centre) or \
                (generation is not None and self.generation is not None and generation != self.generation):
            self.retune(centre=centre)
        if generation is not None:
            self.generation = generation
        n = len(z)
        if t is None:
            t = self._samples / self.rate
        self._samples += n
        t_end = float(t) + n / self.rate
        self._t = t_end
        frame0, X, P = self.stft.feed(z)
        spots: list[dict] = []
        for cands in self.det.add(P):
            ev = self.tracker.update(cands, t_end)
            for tid in ev["forgotten"]:
                for key in [k for k in self._last_char if k[0] == tid]:
                    spots += self._end_over(key, t_end)
                self._forget(tid)
            for young, old in ev["merged"]:
                self.pool.merge(young, old)
                self.calls.forget(young)
            self.pool.assign(self.tracker.tracks, t_end)
        for tid, e in self.pool.run(X, self.tracker.tracks, t_end):
            spots += self._char((tid, e.get("engine", self.pool.engines[0])), e["text"], t_end)
        for key in list(self._last_char):
            if t_end - self._last_char[key] >= QUIET_FLUSH_S:
                spots += self._end_over(key, t_end)
        return spots

    def _char(self, key: tuple, ch: str, t: float) -> list[dict]:
        self._last_char[key] = t
        if ch != " ":
            self._word[key] = self._word.get(key, "") + ch
            return []
        return self._word_done(key, t)

    def _word_done(self, key: tuple, t: float) -> list[dict]:
        tid, engine = key
        w = self._word.pop(key, "")
        tr = self.tracker.tracks.get(tid)
        if not w or tr is None:
            return []
        txt = self.pool.texts_by[engine].get(tid)
        return self.calls.word(tid, w, self.centre + tr.f, t, wpm=txt.wpm if txt else None, snr=tr.snr,
                               band=self.band, engine=engine)

    def _end_over(self, key: tuple, t: float) -> list[dict]:
        tid, engine = key
        out = self._word_done(key, t)
        self._last_char.pop(key, None)
        tr = self.tracker.tracks.get(tid)
        if tr is not None:
            txt = self.pool.texts_by[engine].get(tid)
            out += self.calls.flush(tid, self.centre + tr.f, t, wpm=txt.wpm if txt else None, snr=tr.snr,
                                    band=self.band, engine=engine)
        return out

    # -------------------------------------------------------------- results
    def snapshot(self, t: float | None = None) -> dict:
        """The `cwskim` item (without `t` and `state`, which the app adds):
        the channels strongest first, the carriers, the newest spot rev."""
        t = self._t if t is None else t
        t = 0.0 if t is None else t
        chans, carriers = [], []
        tracks = sorted(self.tracker.tracks.values(), key=lambda tr: -tr.snr)
        for tr in tracks:
            f = self.centre + tr.f
            if tr.carrier:
                carriers.append({"f": round(f, 1), "snr": round(tr.snr, 1)})
                continue
            txt = self.pool.texts.get(tr.id)
            item = {"id": tr.id, "f": round(f, 1), "dial": round(f - self.calls.pitch, 1),
                    "wpm": txt.wpm if txt else None, "snr": round(tr.snr, 1),
                    "text": txt.text[-TEXT_SHOWN:] if txt else "",
                    "first": tr.born, "active": tr.active(t), "decoding": tr.id in self.pool.channels}
            got = self.calls.call_on(tr.id)
            if got:
                item["call"], item["kind"] = got
            chans.append(item)
        return {"band": self.band, "segment": {"lo": self.lo, "hi": self.hi},
                "cover": {"lo": max(self.lo, self.centre - 0.45 * self.rate),
                          "hi": min(self.hi, self.centre + 0.45 * self.rate)},
                "spotsRev": self.calls.rev, "channels": chans[:self.max_channels],
                "carriers": carriers}

    def spots(self, t: float | None = None) -> list[dict]:
        t = self._t if t is None else t
        return self.calls.list(t if t is not None else _time.time())

    @property
    def signals(self) -> int:
        return len(self.tracker.tracks)

    @property
    def decoding(self) -> int:
        return len(self.pool.channels)
