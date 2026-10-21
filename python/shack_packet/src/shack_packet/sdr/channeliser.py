"""A wide IQ capture to one FM audio stream per packet channel.

The SDR captures a couple of megahertz (2.4 MS/s from an RTL-SDR spans
144.8 to 145.825 with room to spare); each packet channel is 12.5 kHz of
it. This picks the channels out, FM-demodulates each, and hands the
receivers (demod.Receiver, one per channel) audio at a rate they like.

**An FFT filter bank, not a mixer and filter per channel.** One forward FFT
of each input block serves every channel: a channel is the few hundred bins
around its frequency, multiplied by the channel filter's response there and
turned back into time with a small inverse FFT, which is the decimation
too (fast-convolution overlap-save, Borgerding's "turning overlap-save into
a multichannel downconversion"). The cost is the big FFT, shared, plus a
tiny one per channel. Measured on shack (Ryzen 9 6900HX, numpy 2.5, one
core, 2.4 MS/s): the shared FFT is about 30 ms a second of IQ at any block
size from 16k to 128k points; a mixer per channel alone (one complex
multiply by an NCO over 2.4 MS/s) is 50 ms a second per channel, before
any filtering, so sixteen channels that way would be a core by themselves.

Choices, each for a reason:

- **Block of N = 512 x D input samples**, D the decimation, overlapping by
  N/8. The channel filter may be as long as the overlap (N/8 + 1 taps:
  8001 at 2.4 MS/s), and that length is what buys the stopband; an
  eighth wastes less of each FFT on overlap than the usual half or quarter
  (a quarter measured 30 % more CPU here for nothing a 2 m packet channel
  needs).
- **Channel IQ at about 19.2 kS/s** (D = 125 at 2.4 MS/s), not 9.6: an FM
  packet signal is about 10.4 kHz wide (Carson: 2 x (3 kHz deviation +
  2.2 kHz tone)), which complex samples at 9.6 kS/s (+-4.8 kHz) would fold.
  `pick_decimation` takes the integer D that gives an even, whole rate
  nearest 19.2 kS/s, so 2.048 MS/s gives 20.48 kS/s. The discriminator's
  audio goes to demod.Receiver at that rate, and the receiver's own
  decimator halves it to its 9.6 kHz (no second audio filter here: one of
  each).
- **The channel filter**: a Kaiser-windowed sinc, flat to +-6 kHz (a
  station 800 Hz off frequency still fits; within 0.2 dB, tested) and,
  measured, 46 dB down at 7.1 kHz (where the next channel's outermost
  sideband starts, 12.5 - 5.2 kHz), 81 dB at 8 kHz and 116 dB at the next
  channel's centre, where nearly all of a neighbour's power is.
  Bins beyond the channel's +-half-rate are never computed, so nothing
  folds in from there.
- **The crystal is corrected here, in software, for every source**: each
  channel's position in the capture is hz / (1 + ppm 1e-6) - centre. A
  fraction of a ppm is as easy as a whole one (librtlsdr takes whole ppm
  only), a Pluto's crystal correction is shared with its transmitter and
  stays untouched (sdr/pluto.py), and an rtl_tcp server may ignore the
  command. Each channel sits on its nearest bin (75 Hz at 2.4 MS/s) and a
  phase-continuous NCO at the channel rate takes out the last few hertz.
- **Each block's phase is put right**: bin c of a block starting at sample
  s has turned through 2 pi c s / N against the first block's; left alone
  that is a jump in phase every block, which the discriminator hears as a
  click forty times a second.
- **The FM discriminator** is the angle between successive samples
  (arg(z[n] z*[n-1])), scaled so +-3 kHz deviation is +-1. A plain
  discriminator, because the receiver behind it has its own AGC and slicers
  for twist; de-emphasis is optional (`deemphasis_us`, 750 us the land-mobile
  value; off by default: a radio's data port sends flat audio, a tracker
  through its mic input sends pre-emphasised, and the receiver's twist
  slicers take +-9 dB an octave either way, demod.py).
- **Level and frequency per block**, for the monitor: the channel's power
  (dBFS) and the sum of z[n] z*[n-1], whose angle over a frame is the
  station's frequency error, power-weighted, so noise between frames does
  not drag it about.

Gaps (IqBlock.dropped) restart the filter's history: the samples either
side do not belong together, and the receivers resynchronise on the next
flag. Audio sample n of channel output maps back to an IQ sample number
through `iq_index(n)`, which allows for the filter's delay and every gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..demod import _Fir
from .plan import Channel
from .source import SdrError

TARGET_RATE = 19_200          # channel IQ rate wanted (Hz)
MIN_CH_RATE = 16_000          # an FM packet channel needs about this much complex bandwidth
MAX_CH_RATE = 32_000
BLOCK_M = 512                 # channel samples per FFT block (N = BLOCK_M x D)
OVERLAP = 8                   # the overlap is N / OVERLAP
PASS_HZ = 6_000.0             # channel filter flat to here...
STOP_HZ = 7_100.0             # ...and falling steeply from here (46 dB at 7.1 kHz, 81 at 8)
KAISER_BETA = 6.5             # about 65 dB of stopband
DEVIATION_HZ = 3_000.0        # 2 m packet's usual deviation: +-1.0 out of the discriminator


def pick_decimation(rate: float) -> tuple[int, int]:
    """(D, channel rate): the integer decimation from `rate` giving a whole,
    even channel rate between 16 and 32 kS/s nearest 19.2 kS/s."""
    r = int(round(rate))
    if abs(rate - r) > 1e-6:
        raise SdrError("rate-unsupported", f"{rate} S/s is not a whole number")
    best = None
    for d in range(max(1, r // MAX_CH_RATE), r // MIN_CH_RATE + 1):
        if r % d or (r // d) % 2 or not MIN_CH_RATE <= r // d <= MAX_CH_RATE:
            continue
        if best is None or abs(r // d - TARGET_RATE) < abs(r // best - TARGET_RATE):
            best = d
    if best is None:
        raise SdrError("rate-unsupported", f"no whole channel rate near {TARGET_RATE} S/s divides {r} S/s")
    return best, r // best


def _kaiser_lowpass(taps: int, cutoff: float, rate: float, beta: float) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * cutoff / rate * np.sinc(2.0 * cutoff / rate * n) * np.kaiser(taps, beta)
    return h / h.sum()


def _deemphasis(tau_s: float, rate: float) -> np.ndarray:
    """A one-pole de-emphasis (6 dB an octave above 1 / 2 pi tau) as an FIR:
    its impulse response cut at eight time constants, unity at DC. An FIR
    because a recursive filter is a Python loop a sample; this is one
    numpy convolution a block."""
    k = np.exp(-np.arange(int(math.ceil(8 * tau_s * rate)) + 1) / (tau_s * rate))
    return k / k.sum()


@dataclass
class Chunk:
    """What one feed() produced. `audio[k]` is channel k's audio from audio
    sample `start`; `power[k, b]` and `corr[k, b]` cover audio samples
    start + b * per_block onward (per_block each)."""
    start: int
    audio: np.ndarray        # (channels, n) float64
    power: np.ndarray        # (channels, blocks) mean |z|^2, full scale 1.0
    corr: np.ndarray         # (channels, blocks) complex sum of z[n] z*[n-1]
    per_block: int


class Channeliser:
    def __init__(self, rate: float, centre: float, channels, *, ppm: float = 0.0,
                 deemphasis_us: float | None = None, deviation_hz: float = DEVIATION_HZ,
                 decimation: int | None = None, pass_hz: float = PASS_HZ, stop_hz: float = STOP_HZ):
        """`decimation`, `pass_hz` and `stop_hz` are for a channel wider than
        a 1200 baud one (the fast FSK modes, radios.pluto): a decimation the
        caller chose, so the channel rate is the modem's own, and the
        filter's edges. The defaults are the 12.5 kHz packet channel above."""
        self.rate = float(rate)
        self.centre = float(centre)
        self.channels: list[Channel] = list(channels)
        if decimation is None:
            self.D, self.ch_rate = pick_decimation(rate)
        else:
            r = int(round(rate))
            if r % int(decimation):
                raise SdrError("rate-unsupported", f"{decimation} does not divide {r} S/s")
            self.D, self.ch_rate = int(decimation), r // int(decimation)
        self.pass_hz, self.stop_hz = float(pass_hz), float(stop_hz)
        self.M = BLOCK_M
        self.N = self.M * self.D
        self.V = self.N // OVERLAP              # overlap, a multiple of D because M is of OVERLAP
        self.hop = self.N - self.V
        self.skip = self.V // self.D             # outputs of each block that overlap the last
        self.per_block = self.hop // self.D      # new outputs a block
        self.bin_hz = self.rate / self.N
        h = _kaiser_lowpass(self.V + 1, (self.pass_hz + self.stop_hz) / 2.0, self.rate, KAISER_BETA)
        H = np.fft.fft(h, self.N)
        self._korder = np.concatenate([np.arange(self.M // 2), np.arange(-self.M // 2, 0)])
        # 1/D: the small inverse FFT's 1/M against the full one's 1/N.
        self._H = (H[self._korder % self.N] / self.D).astype(np.complex64)
        self.deviation_hz = float(deviation_hz)
        self._deemph = None
        if deemphasis_us:
            k = _deemphasis(deemphasis_us * 1e-6, self.ch_rate)
            self._deemph = [_Fir(k) for _ in self.channels]
        self.ppm = 0.0
        self.set_ppm(ppm)
        self._reset_stream()
        self.blocks = 0                          # FFT blocks since the start (for the phase)
        self.out_n = 0                           # audio samples produced
        # Where each run of audio began: (audio sample, IQ sample of that
        # audio sample's centre). A new one after every gap.
        self._anchors: list[tuple[int, int]] = [(0, -self.V // 2)]
        self._iq_next = 0                        # IQ index the next fed sample should have

    # -- tuning --------------------------------------------------------------

    def offset_of(self, hz: float) -> float:
        """Where `hz` on the air sits in the capture, Hz from its centre."""
        return hz / (1.0 + self.ppm * 1e-6) - self.centre

    def set_ppm(self, ppm: float) -> None:
        """The receiver's crystal error. Takes effect from the next block;
        the channels' phase carries on, so a change mid-frame costs that
        frame at most."""
        self.ppm = float(ppm)
        bins, res = [], []
        for ch in self.channels:
            f = self.offset_of(ch.hz)
            if abs(f) + self.stop_hz > self.rate / 2.0:
                raise SdrError("out-of-range", f"{ch.hz / 1e6:.4f} MHz is outside "
                               f"{self.centre / 1e6:.4f} MHz +- {self.rate / 2e6:.3f} MHz")
            c = int(round(f / self.bin_hz))
            bins.append(c)
            res.append(f - c * self.bin_hz)
        self._bins = np.array(bins, np.int64)
        self._idx = (self._bins[:, None] + self._korder[None, :]) % self.N       # (K, M)
        self._res = np.array(res)
        if not hasattr(self, "_nco"):
            self._nco = np.zeros(len(self.channels))                              # radians

    def _reset_stream(self) -> None:
        self._buf = np.zeros(self.V, np.complex64)
        self._prev = np.ones(len(self.channels), np.complex64)  # last channel sample, for the discriminator

    # -- the stream ----------------------------------------------------------

    def feed(self, z: np.ndarray, index: int | None = None) -> Chunk:
        """IQ in (complex64, full scale 1.0); audio for every channel out.
        `index` is the IQ sample number of z[0] (IqBlock.index): a jump
        from where the last block ended is a gap."""
        if index is not None and index != self._iq_next:
            self._gap(index)
        self._iq_next = (index if index is not None else self._iq_next) + len(z)
        buf = np.concatenate([self._buf, np.asarray(z, np.complex64)])
        B = (len(buf) - self.V) // self.hop
        K = len(self.channels)
        start = self.out_n
        if B <= 0:
            self._buf = buf
            return Chunk(start, np.zeros((K, 0)), np.zeros((K, 0)), np.zeros((K, 0), np.complex128),
                         self.per_block)
        win = np.lib.stride_tricks.sliding_window_view(buf, self.N)[::self.hop][:B]
        X = np.fft.fft(win, axis=1)                                   # (B, N), complex64
        self._buf = buf[B * self.hop:]
        Y = X[:, self._idx] * self._H                                 # (B, K, M)
        y = np.fft.ifft(Y, axis=2)[:, :, self.skip:]                  # (B, K, per_block)
        # Each block's phase, against the first block's (module docstring).
        g = self.blocks + np.arange(B)
        turns = (self._bins[None, :] * (g[:, None] * self.hop % self.N)) % self.N
        y = y * np.exp(-2j * np.pi * turns / self.N)[:, :, None].astype(np.complex64)
        self.blocks += B
        y = np.ascontiguousarray(y.transpose(1, 0, 2)).reshape(K, B * self.per_block)
        # The last few hertz: a phase-continuous NCO per channel.
        n = y.shape[1]
        step = -2.0 * np.pi * self._res / self.ch_rate
        ph = self._nco[:, None] + step[:, None] * np.arange(n)[None, :]
        y = y * np.exp(1j * ph).astype(np.complex64)
        self._nco = (self._nco + step * n) % (2.0 * np.pi)
        # The discriminator.
        prev = np.concatenate([self._prev[:, None], y[:, :-1]], axis=1)
        prod = y * np.conj(prev)
        self._prev = y[:, -1].copy()
        audio = np.angle(prod).astype(np.float64) * (self.ch_rate / (2.0 * np.pi * self.deviation_hz))
        if self._deemph is not None:
            audio = np.stack([f(a) for f, a in zip(self._deemph, audio)])
        power = (np.abs(y) ** 2).reshape(K, B, self.per_block).mean(axis=2)
        corr = prod.reshape(K, B, self.per_block).sum(axis=2).astype(np.complex128)
        self.out_n += n
        return Chunk(start, audio, power, corr, self.per_block)

    def _gap(self, index: int) -> None:
        # What is waiting in the buffer (less than a block) is dropped with
        # the history: it belongs to before the gap.
        self._reset_stream()
        self._anchors.append((self.out_n, index - self.V // 2))
        del self._anchors[:-64]

    def iq_index(self, audio_n: float) -> float:
        """The IQ sample number that audio sample `audio_n` came from."""
        for n0, i0 in reversed(self._anchors):
            if audio_n >= n0:
                return i0 + (audio_n - n0) * self.D
        n0, i0 = self._anchors[0]
        return i0 + (audio_n - n0) * self.D

    def freq_hz(self, corr: complex) -> float:
        """A channel's frequency error from a sum of corr values."""
        return float(np.angle(corr)) * self.ch_rate / (2.0 * np.pi)
