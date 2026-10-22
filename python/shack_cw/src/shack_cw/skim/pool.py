"""The decoder pool: one shack_cw.Decoder per decoded signal, fed from the
filter bank through Decoder.feed_frames.

Channels go to tracks in order of SNR, up to `max_channels` (cwSkimMax:
64 by default, 256 at most, Brian 13/10/2026). A track stronger than the
weakest decoded one takes its channel only after outranking it for
SWAP_S, so decoders do not thrash in a busy band; a track losing its
channel keeps its text. Carriers and quiet tracks get none.

Per block the work is: one gather of seven bins per channel
(frontend.Gather), the coherent window and side medians vectorised over
every channel that shares a window length (shack_cw.amplitudes, the
function Decoder.feed itself runs), then each decoder's frame loop - the
only Python per signal, about 0.26 % of a core each (CW-SKIMMER-PLAN §2.1).

Each decoder runs with afc=False (the tracker steers the frequency; AFC's
+-100 Hz would pull two decoders onto one strong station in a pileup) and
polarity=False (HF has no F1A beacons: GUIDE §5).

With engine="nn" the decoders are one shack_cw.nn.Bank: every channel's
seven sums go through the network in one call a block, which is what
keeps a neural back end inside the skimmer's CPU budget (the network's
arithmetic is a few small matrix products; per channel and per block,
Python would cost more than the network does).

With engine="both" (Brian, 14/10/2026) every channel has both: the Bank
and a classic Decoder, fed from the same gather, each printing its own
text. The CW survey of the IQ library (office
/srv/iq/derived/cw-survey/REPORT.md) found the RBN's call in the text of
one engine or the other in 888 of 902 windows, where the network alone
had 792 and the classic engine 675: the network reads fists, speed and
fading better, the classic engine reads through a second station on the
frequency. A switch between them gains 1 %; the union gains 12 %, and
needs the callsign check and seen-twice in calls.py to keep it clean.
The first engine in ENGINE_SETS is the primary one: its text is the one
a snapshot shows, and its speed the one spots carry.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..decoder import REF_OFFSETS, Decoder, amplitudes
from ..i18n import _
from .frontend import Gather, Stft

SWAP_S = 5.0
TEXT_KEEP = 200          # characters of text kept per track
# What each `engine` setting runs on a channel, primary first.
ENGINE_SETS = {"classic": ("classic",), "nn": ("nn",), "both": ("nn", "classic")}
# The neural engine's language model (shack_cw.nn.lm) in the skimmer's
# channels only (Brian, 14/10/2026): callsigns and CW phrases weighed
# against what the network heard, at the weight its measurements chose.
# The live readers in Radio, Icom and Flex keep nn.LM's default (off, the
# greedy reading): the search prints up to 0.5 s later, which a spot list
# never notices and an operator reading along would.
SKIM_LM = 0.5
# With both engines, the classic engine reads at most this many channels:
# the first ones given out, which are the strongest signals at the time
# (assign() hands channels out strongest first), and a slot freed by a
# channel that goes is given to the next channel made. In Radio's image,
# one CPU, every channel busy (bench cpu, 14/10/2026): the front end 10 %
# of a core on ohs-box, a classic channel 0.33 %, a network channel with
# the LM 0.36 % on the box (reference BLAS) and 0.18 % with OpenBLAS.
# Both engines on all 256 measured 160-175 % of a core, more than the one
# worker process has, so it would drop blocks. The classic
# engine's gain is reading through a second station on the frequency,
# which matters most on the strong signals; the network reads them all.
CLASSIC_MAX = 64


@dataclass
class Text:
    """What has been decoded on one track: characters with the time each
    came out (seconds, the skimmer's clock), and the speeds."""
    chars: deque = field(default_factory=lambda: deque(maxlen=TEXT_KEEP))
    times: deque = field(default_factory=lambda: deque(maxlen=TEXT_KEEP))
    wpm: float | None = None

    @property
    def text(self) -> str:
        return "".join(self.chars)


class Channel:
    def __init__(self, squelch_db: float, classic: bool = True):
        # The neural engine's channels live in the pool's Bank; this one
        # keeps only the place in the pool (and the classic decoder when
        # the classic engine runs).
        self.decoder = Decoder(None, freq=0.0, afc=False, snr_db=squelch_db, polarity=False) \
            if classic else None
        n = len(REF_OFFSETS)
        # A full history of zeros, so every channel of a group has the same
        # shape to stack (amplitudes keeps 8 frames); a new channel's first
        # few frames read a little low, which the decoder's acquisition
        # does not notice.
        self.hist = (np.zeros((8, n), np.complex128), np.zeros((8, n)))


class Pool:
    def __init__(self, stft: Stft, max_channels: int = 64, squelch_db: float = 6.0, engine: str = "both"):
        if engine not in ENGINE_SETS:
            raise ValueError(_("unknown CW engine {engine!r}: one of {engines}").format(
                engine=engine, engines=", ".join(ENGINE_SETS)))
        self.stft = stft
        self.max_channels = int(max_channels)
        self.engine = engine
        self.engines = ENGINE_SETS[engine]
        # Channels the classic engine may read: all of them when it runs
        # alone, CLASSIC_MAX beside the network.
        self.classic_max = (CLASSIC_MAX if len(self.engines) > 1 else 1 << 30) if "classic" in self.engines else 0
        self.bank = None
        if "nn" in self.engines:
            from ..nn import Bank
            self.bank = Bank(float(squelch_db))
            self.bank.lm = SKIM_LM
        # The neural engine's weights say which bins they read (the tone,
        # REF_OFFSETS and perhaps more beside the tone).
        self.gather = Gather(stft, self.bank.offsets if self.bank is not None else REF_OFFSETS)
        self._squelch_db = float(squelch_db)
        self.channels: dict[int, Channel] = {}
        # Each engine's text per track; `texts` is the primary engine's.
        self.texts_by: dict[str, dict[int, Text]] = {e: {} for e in self.engines}
        self.texts: dict[int, Text] = self.texts_by[self.engines[0]]
        self._outrank: dict[int, float] = {}      # unassigned track -> since when it beat the weakest

    @property
    def squelch_db(self) -> float:
        return self._squelch_db

    @squelch_db.setter
    def squelch_db(self, db: float) -> None:
        """Every decoder's squelch, and any channel made from now on."""
        self._squelch_db = float(db)
        for ch in self.channels.values():
            if ch.decoder is not None:
                ch.decoder.snr_db = self._squelch_db
        if self.bank is not None:
            self.bank.snr_db = self._squelch_db

    def drop(self, tid: int, forget: bool = False) -> None:
        self.channels.pop(tid, None)
        self.gather.drop(tid)
        self._outrank.pop(tid, None)
        if forget:
            for texts in self.texts_by.values():
                texts.pop(tid, None)

    def merge(self, young: int, old: int) -> None:
        """A track folded into an older one: its channel goes; the older
        keeps its own text and call."""
        self.drop(young, forget=True)

    def reset(self) -> None:
        for tid in list(self.channels):
            self.drop(tid)

    def assign(self, tracks: dict, t: float) -> None:
        """Give channels to the strongest eligible tracks (see the module).

        A track gone quiet keeps its decoder while no active track needs
        the channel. A station that calls CQ for ten seconds and listens
        for twenty is quiet in between; on the box's 20 m recording,
        dropping its decoder there meant every over was decoded by a new
        Decoder that had to learn the speed again and lost the first
        words - the CQ and the call."""
        live = [tr for tr in tracks.values() if not tr.carrier]
        active = sorted((tr for tr in live if tr.active(t)), key=lambda tr: -tr.snr)
        keep = {tr.id for tr in live}
        for tid in list(self.channels):
            if tid not in keep:
                self.drop(tid)
        for tid in list(self._outrank):
            if tid not in keep or tid in self.channels or not tracks[tid].active(t):
                self._outrank.pop(tid, None)
        waiting = [tr for tr in active if tr.id not in self.channels]
        if self.max_channels <= 0:        # the bench's front end alone
            return
        for tr in waiting:
            if len(self.channels) < self.max_channels:
                self._add(tr.id)
                continue
            # Full: a quiet track's channel goes first, the longest quiet.
            quiet = [tracks[k] for k in self.channels if not tracks[k].active(t)]
            if quiet:
                self.drop(min(quiet, key=lambda x: x.seen).id)
                self._add(tr.id)
                continue
            weakest = min((tracks[k] for k in self.channels), key=lambda x: x.snr)
            if tr.snr > weakest.snr:
                since = self._outrank.setdefault(tr.id, t)
                if t - since >= SWAP_S:
                    self.drop(weakest.id)
                    self._add(tr.id)
            else:
                self._outrank.pop(tr.id, None)

    def _add(self, tid: int) -> None:
        classic = sum(1 for ch in self.channels.values() if ch.decoder is not None) < self.classic_max
        self.channels[tid] = Channel(self.squelch_db, classic)
        for texts in self.texts_by.values():
            texts.setdefault(tid, Text())

    def run(self, X: np.ndarray, tracks: dict, t_end: float) -> list[tuple[int, dict]]:
        """Drive every channel's decoders with the frames X; the decoders'
        events as (track id, event), each stamped `t` (the block's end) and
        `engine` (which engine printed it)."""
        out: list[tuple[int, dict]] = []
        if not len(X) or not self.channels:
            return out
        keys = list(self.channels)
        c = self.gather.take(X, keys, [tracks[k].f for k in keys])
        if self.bank is not None:
            out += self._run_nn(c, keys, t_end)
        if self.classic_max:
            idx = [i for i, k in enumerate(keys) if self.channels[k].decoder is not None]
            if idx:
                out += self._run_classic(c[:, idx], [keys[i] for i in idx], t_end)
        return out

    def _run_classic(self, c: np.ndarray, keys: list[int], t_end: float) -> list[tuple[int, dict]]:
        out: list[tuple[int, dict]] = []
        texts = self.texts_by["classic"]
        groups: dict[int, list[int]] = {}
        for i, k in enumerate(keys):
            groups.setdefault(self.channels[k].decoder.window_frames, []).append(i)
        for m, idx in groups.items():
            chans = [self.channels[keys[i]] for i in idx]
            hist = (np.stack([ch.hist[0] for ch in chans], axis=1), np.stack([ch.hist[1] for ch in chans], axis=1))
            # The classic engine reads REF_OFFSETS, the first columns of a
            # gather made for the network's front end (which may add bins).
            tone, quiet, loud, (h0, h1) = amplitudes(c[:, idx, :len(REF_OFFSETS)], m, hist)
            for j, (i, ch) in enumerate(zip(idx, chans)):
                ch.hist = (h0[:, j], h1[:, j])
                ev = ch.decoder.feed_frames(tone[:, j], quiet[:, j], loud[:, j])
                if not ev:
                    continue
                tid = keys[i]
                txt = texts[tid]
                for e in ev:
                    txt.chars.append(e["text"])
                    txt.times.append(t_end)
                    out.append((tid, dict(e, t=t_end, engine="classic")))
                if ch.decoder.wpm:
                    txt.wpm = ch.decoder.wpm
        return out

    def _run_nn(self, c: np.ndarray, keys: list[int], t_end: float) -> list[tuple[int, dict]]:
        out: list[tuple[int, dict]] = []
        texts = self.texts_by["nn"]
        chars = self.bank.run(c, keys)
        for j, tid in enumerate(keys):
            w = float(self.bank.wpm[j])
            txt = texts[tid]
            if w > 0:
                txt.wpm = round(w, 1)
            for ch in chars[tid]:
                # The classic engine's event shape (Decoder._emit).
                e = {"mode": "cw", "text": ch, "freq": 0.0, "t": t_end, "engine": "nn"}
                if ch != " " and w > 0:
                    e["wpm"] = round(w, 1)
                txt.chars.append(ch)
                txt.times.append(t_end)
                out.append((tid, e))
        return out
