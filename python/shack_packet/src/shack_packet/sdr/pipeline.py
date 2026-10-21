"""IQ blocks in, decoded frames and monitor tables out: the SDR monitor's
work, on whatever thread calls `process` (sdr/runner.py gives it one).

    pipe = Pipeline(source.rate, source.centre, plan, ppm=1.5)
    for block in iter(source.read, None):
        for d in pipe.process(block):          # also in pipe.monitor
            print(d.channel, d.text, d.level_db)
    pipe.flush()

One demod.Receiver per channel, fed the channeliser's audio. `diversity`
is the receiver's: both demodulators (the best decoding, about 20 ms of
CPU a channel-second) or one (about 10 ms; within a few frames of both on
real recordings, behind on steep twist - demod.py). "auto", the default,
runs both while there are at most AUTO_DIVERSITY_MAX channels and one
beyond, so sixteen channels stay well inside one core (the measured
figures are in the README).

Each frame's level, SNR and frequency error are measured over the frame's
own airtime from the channeliser's per-block power and phase: its level in
dBFS, the SNR against that channel's noise floor (the 10th percentile of
its block powers over the last FLOOR_S: the level the channel sits at
between transmissions), and its frequency error as the angle of the summed
lag products (channeliser.py).
"""

from __future__ import annotations

import collections
import math

import numpy as np

from .. import aprs, ax25
from ..ax25 import PID_NONE, FrameType
from ..demod import Receiver
from ..fx25 import TAGS
from ..netrom import parse_nodes
from .channeliser import Channeliser
from .memory import keep_heap
from .monitor import Decoded, Monitor, airtime_s
from .plan import ChannelPlan
from .source import IqBlock
from .spectrum import Spectrum

_Levels = collections.namedtuple("_Levels", "start n power corr per_block")

AUTO_DIVERSITY_MAX = 8
FLOOR_S = 30.0            # noise floor from this much history
FLOOR_EVERY_S = 1.0
APRS_PID = PID_NONE       # APRS is UI with "no layer 3", 0xF0


class Pipeline:
    def __init__(self, rate: float, centre: float, plan: ChannelPlan, monitor: Monitor | None = None, *,
                 ppm: float = 0.0, diversity="auto", deemphasis_us: float | None = None, spectrum: dict | None = None):
        # Before the first block: without it a process can spend twice the
        # channeliser's own CPU on page faults (memory.py has the numbers).
        self.heap_kept = keep_heap()
        self.rate = float(rate)
        self.centre = float(centre)
        self.plan = plan
        self.monitor = monitor or Monitor(plan)
        self.ch = Channeliser(rate, centre, list(plan), ppm=ppm, deemphasis_us=deemphasis_us)
        if diversity == "auto":
            diversity = len(plan) <= AUTO_DIVERSITY_MAX
        self.diversity = bool(diversity)
        self.receivers = [Receiver(self.ch.ch_rate, diversity=self.diversity) for _ in plan]
        self.spectrum = None if spectrum is False else Spectrum(rate, centre, **(spectrum or {}))
        if self.spectrum is not None:
            self.monitor.spectrum_freqs = self.spectrum.freqs()
        self.t0: float | None = None
        self._hist: collections.deque = collections.deque()      # recent Chunks, FLOOR_S of them
        self._floor = [None] * len(plan)
        self._floor_due = 0.0
        self._audio_s = 0.0

    @property
    def ppm(self) -> float:
        return self.ch.ppm

    def set_ppm(self, ppm: float) -> None:
        self.ch.set_ppm(ppm)

    def _time(self, audio_n: float) -> float:
        return self.t0 + self.ch.iq_index(audio_n) / self.rate

    def process(self, block: IqBlock) -> list[Decoded]:
        if self.t0 is None:
            self.t0 = block.time - block.index / self.rate
        chunk = self.ch.feed(block.samples, block.index)
        if self.spectrum is not None:
            self.monitor.add_rows(self.spectrum.feed(block.samples, block.index, block.time),
                                  self.spectrum.freqs())
        n = chunk.audio.shape[1]
        if not n:
            return []
        # The levels only, not the audio: thirty seconds of sixteen
        # channels' audio would be 70 MB.
        self._hist.append(_Levels(chunk.start, n, chunk.power, chunk.corr, chunk.per_block))
        keep = FLOOR_S * self.ch.ch_rate
        while self._hist[0].start + self._hist[0].n < chunk.start + n - keep:
            self._hist.popleft()
        self._levels(self._hist[-1])
        out: list[Decoded] = []
        dur = n / self.ch.ch_rate
        when = self._time(chunk.start)
        for k, (c, rx) in enumerate(zip(self.plan, self.receivers)):
            heard = rx.feed(chunk.audio[k])
            if rx.dcd:
                self.monitor.busy(c.id, when, dur)
            out += [self._decoded(k, h) for h in heard]
        for d in out:
            self.monitor.add(d)
        return out

    def flush(self) -> list[Decoded]:
        out = []
        for k, rx in enumerate(self.receivers):
            out += [self._decoded(k, h) for h in rx.flush()]
        for d in out:
            self.monitor.add(d)
        return out

    # -- measuring --------------------------------------------------------------

    def _levels(self, chunk) -> None:
        self._audio_s += chunk.n / self.ch.ch_rate
        last = chunk.power[:, -1]
        if self._audio_s >= self._floor_due:
            self._floor_due = self._audio_s + FLOOR_EVERY_S
            p = np.concatenate([c.power for c in self._hist], axis=1)
            self._floor = list(10 * np.log10(np.percentile(p, 10, axis=1) + 1e-20))
        for k, c in enumerate(self.plan):
            self.monitor.levels(c.id, float(10 * np.log10(last[k] + 1e-20)), self._floor[k])

    def _span(self, k: int, n0: float, n1: float) -> tuple[float | None, complex]:
        pw, corr, count = 0.0, 0j, 0
        for c in self._hist:
            b0 = max(0, int((n0 - c.start) // c.per_block))
            b1 = min(c.power.shape[1], int(math.ceil((n1 - c.start) / c.per_block)))
            if b1 > b0:
                pw += float(c.power[k, b0:b1].sum())
                corr += complex(c.corr[k, b0:b1].sum())
                count += b1 - b0
        return (pw / count if count else None), corr

    def _decoded(self, k: int, h) -> Decoded:
        c = self.plan.channels[k]
        frame = ax25.parse(h.frame)
        span = airtime_s(h.frame, h.fx25) * self.ch.ch_rate
        power, corr = self._span(k, h.sample, h.sample + span)
        level = 10 * math.log10(power + 1e-20) if power is not None else None
        floor = self._floor[k]
        t = self._time(h.sample)
        d = Decoded(self.monitor.next_seq(), t, c.id, c.hz, h.frame, frame, level,
                    None if level is None or floor is None else level - floor,
                    self.ch.freq_hz(corr) if corr else None, h.fx25,
                    TAGS[h.fx25].nroots if h.fx25 in TAGS else None, h.corrected)
        if frame is not None and frame.type is FrameType.UI:
            d.nodes = parse_nodes(frame)
            if d.nodes is None and frame.pid == APRS_PID:
                d.aprs = aprs.parse(frame, now=t)
        return d
