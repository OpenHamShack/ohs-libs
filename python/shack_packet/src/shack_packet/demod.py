"""Bell 202 audio in, frames out: the receive side of the modem.

The radio has already FM-demodulated the signal, so what arrives is two audio
tones taking turns, 1200 Hz for a mark and 2200 Hz for a space, 1200 times a
second, under hiss, with whatever twist the radio's audio path gave them.

This replaces the Icom app's demodulator (afsk.py there). Measured on shack
against direwolf 1.7, 17/10/2026 (`python -m shack_packet.bench`):

| | Icom | here | direwolf atest | atest -P E+ |
|---|---|---|---|---|
| gen_packets -n 30 ramps, 5 amplitudes, 12 kHz (of 150) | 36 | 61 | 54 | 57 |
| our noise and twist set (of 600) | 13 | 559 | - | - |
| WA8LMF TNC Test CD track 1 (flat) | - | 1008 | 1005 | 1015 |
| WA8LMF track 2 (de-emphasised) | - | 988 | 983 | 1002 |
| WA8LMF track 4 | - | 107 | 102 | 107 |

Our set is 600 frames in white noise at 3-5 dB SNR in a 6 kHz band under
twist from -9 to +9 dB an octave: the ramps are flat audio, and real radios
are not. The WA8LMF CD is real 144.39 traffic off the air (a dev-only
download; never committed). So: ahead of direwolf's default everywhere,
level with or a little behind its nine-slicer `E+` on real recordings.

Every stage below was measured, and the numbers are with each stage. They
come from the search as it went - each measured with the stages before it
in place - so they compare the alternatives at that step and do not add up.

**Two front ends, side by side.** Each tone is mixed to 0 Hz and low-passed;
the two front ends differ in how.

*The Icom one*: no prefilter, and a two-bit Hann window as the low-pass -
the matched filter the Icom code used, now a numpy convolution. Two bits,
Hann, beat 1.5 bits (-20 % of our set), 2.5 bits (-27 %) and sine or
Gaussian windows (-18 to -23 %). *Amplitudes, not powers*: they decode the
same and the twist slicers below space more evenly on them.

*direwolf 1.7's profile A*: a band-pass 0.155 baud outside the two tones
(1014-2386 Hz), eight bits long, then a root-raised-cosine low-pass 2.8 bits
long with roll-off 0.2. It is the better one on real recordings (WA8LMF
tracks 1 and 2: 1005 and 984 alone, against 1000 and 957 for the best
short 800-2600 Hz prefilter we tried) and the worse one under steep twist,
where its long filters ring the strong tone into the weak one's bits (our
set: 425 alone).

Together, a frame either decodes counting once: 1008, 988, 559, 61. Both
cost twice one (about 20 ms of CPU a second of audio against 9);
`Receiver(diversity=False)` runs the direwolf-style one alone (1008, 986,
450, 60). Before the direwolf-style front end the pair was the Icom one and
a 600-2800 Hz prefilter, 2.5 bits long, ahead of the Hann window: 996, 950,
536, 60.

**Each tone normalised by its own peak and valley (AGC).** Twist - one tone
louder than the other, which every FM radio's de-emphasis or pre-emphasis
introduces, by anything up to 9 dB - moves the right decision point away from
"equal amplitudes". Following each tone's peak and valley and scaling both
to the same swing takes it out, whatever the radio did. Attack fast (0.7 a
half-bit), decay slowly (over 200 bits): a decay of 10 bits followed the
noise and lost half the frames; 30 bits 448, 100 bits 505, 200 and 400 bits
517 of 600 on our set. Without the AGC, 258. Real traffic disagrees at the
long end - on WA8LMF track 2, where stations of every level follow each
other, 400 bits lost 24 frames against 200 and 50 bits gained 10 - so 200 is
the compromise.

**Seven slicers on different twist assumptions**, as direwolf does: the
decision is `mark - g * space` for g from 0.35 to 2.8, log-spaced, each slicer
with its own bit clock and framer, a frame from any of them counting once.
After the AGC they mop up what it gets wrong. On our set (with the first
front-end pair): one slicer 305, three 448, five 517, seven 534, nine no
better than seven. The range: 0.5-2 was enough for our set; the WA8LMF
tracks wanted wider (0.35-2.8: +5 frames on track 2, +6 on our set, against
0.5-2).

**The bit clock is a PLL that only wakes on transitions.** It is the Icom
code's DPLL, pulled toward sampling half a bit after each transition - but
stepped from one transition to the next arithmetically rather than sample by
sample, because between transitions nothing happens but the phase advancing
and whole bits falling out of it. The pull keeps 0.85 of the error each time
(0.5 lost 4 %; 0.7-0.9 were within a frame of each other). *And it is second
order*: the error also nudges the loop's idea of the baud rate (gain 0.005,
within 3 %, drawn back to 1200 by 0.002 a transition). A first-order loop
sits off-centre by an amount proportional to the sender's clock error, and
in noise that is fatal: at 3-5 dB with the sender 1 % fast it decoded 304 of
600 frames against 502 at 0 %, and at 2 % slow, 4. With the rate tracked:
500 at 0 %, 473 at +1 %, 492 at -2% (one demodulator). Real stations' clocks
differ too: without the rate tracking, WA8LMF track 2 fell from 950 to 914.
With the final receiver, our set with the sender -2/-1/0/+1/+2 % out:
559/557/559/533/537. direwolf's `gen_packets -v` sweep (-2 % to +2 %,
clean) decodes 9 of 9.

What did not help, so nobody tries it again: a peak/valley slicer on the
difference signal (the Icom design) under noise, 40 frames against 55 for a
plain zero threshold - its peaks are the noise's; separate "locked" and
"searching" PLL gains (direwolf's 0.74/0.50) - no different from one gain;
a third demodulator with a 1.5-bit window - one more frame in 600, for 50 %
more CPU; a third with an 800-2600 Hz prefilter - 1, 3 and 5 more frames
(tracks 1 and 2, our set) for 50 % more CPU.

**Decimated to about 10 kHz first.** The demodulator has nothing to gain
above 3 kHz, and its cost is per sample, so 44.1 and 48 kHz are decimated by
4 and 5 (to 11.025 and 9.6 kHz) behind an anti-alias filter. 8 to 16 kHz run
as they come. The WA8LMF tracks (44.1 kHz) decode the same undecimated
(1009 and 988) at four times the cost.

**FX.25 on every slicer.** Each slicer's bits also go through an FX.25
receiver (fx25.py); a frame sent with FX.25 is decoded by both, and the
FX.25 copy is the one reported (the plain copy is held while a block is
being collected) so the app can say it was corrected.

**Block-exact.** Audio arrives in fragments with no relation to anything,
so the receiver works in fixed blocks of its own and the result is the same
however the audio is cut - tested one sample at a time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import fx25, hdlc
from .afsk import BAUD, MARK_HZ, SPACE_HZ
from .errors import PacketError

#: How long the window of each tone's matched filter is, in bits.
WINDOW_BITS = 2.0

#: The twist slicers' space-tone gains.
GAINS = tuple(float(g) for g in np.geomspace(0.35, 2.8, 7))

#: The AGC: how much of the way to a new peak each half-bit goes, and over
#: how many bits it forgets one.
AGC_ATTACK = 0.7
AGC_DECAY_BITS = 200.0

#: The bit clock: how much of its phase error survives a transition; how
#: hard that error steers its idea of the baud rate; how far from 1200 the
#: rate may wander; and how hard it is drawn back to 1200 (so a long stretch
#: of noise between transmissions leaves it where the next one expects).
PLL_INERTIA = 0.85
PLL_FREQ_GAIN = 0.005
PLL_FREQ_RANGE = 0.03
PLL_FREQ_LEAK = 0.002

#: Data carrier detect, as direwolf does it: a transition within DCD_WIDTH
#: of a bit boundary (where the bit clock expects one) is "good", and the
#: receiver has carrier from when some slicer's last DCD_WINDOW transitions
#: hold DCD_ON good ones until every slicer's hold DCD_OFF or fewer. Noise
#: puts transitions anywhere; AFSK puts nearly all of them on the clock. A
#: stretch longer than DCD_GAP_BITS with no transition at all (a closed
#: squelch, a steady carrier) cannot be HDLC, which stuffs a transition at
#: least every six bits, and clears it. Measured on shack, 17/10/2026, at
#: 12 kHz in 20 ms blocks (white noise; twenty 30 s runs, and four SNRs x
#: five runs of a 300 ms preamble): no false carrier in ten minutes; carrier
#: a median 100 ms into a transmission, 300 at worst at 3 dB SNR, so inside
#: its TXDELAY. A 16-transition window (14 on) was no faster and gave three
#: false blocks in the ten minutes; 15 of 16 and 20 of 24 were quiet but
#: 20-50 ms slower. (A flag preamble has only two transitions a byte, so
#: any window takes tens of milliseconds to fill.)
DCD_WIDTH = 0.15
DCD_WINDOW = 32
DCD_ON = 24
DCD_OFF = 15
DCD_GAP_BITS = 12

#: The prefilter's length in bits, unless a demodulator says otherwise.
PREFILTER_BITS = 2.5

#: The demodulators side by side: each one's prefilter band (None for none),
#: its AGC's decay in bits, its prefilter's length in bits, and its tone
#: low-pass - None for the two-bit Hann window, or a root-raised cosine's
#: (width in bits, roll-off). `Receiver(diversity=False)` runs the first.
#: The first is direwolf 1.7's profile A front end (band = the tones -+0.155
#: baud, 8 bits long; RRC 2.8 bits, roll-off 0.2), the second the Icom one.
DEMODS = (
    ((MARK_HZ - 0.155 * BAUD, SPACE_HZ + 0.155 * BAUD), AGC_DECAY_BITS, 8.0, (2.8, 0.2)),
    (None, AGC_DECAY_BITS, None, None),
)

#: Lowest rate the decimator will go down to.
MIN_RATE = 9600


def _bandpass(rate: float, lo: float, hi: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0

    def lowpass(fc):
        return 2.0 * fc / rate * np.sinc(2.0 * fc / rate * n)

    return (lowpass(hi) - lowpass(lo)) * np.hamming(taps)


def _hann(length: int) -> np.ndarray:
    k = (np.arange(length) + 0.5) / length
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * k)


def _rrc(sps: float, width_bits: float, rolloff: float) -> np.ndarray:
    """A root-raised-cosine low-pass, `width_bits` long."""
    length = max(4, int(round(sps * width_bits)))
    t = (np.arange(length) - (length - 1) / 2.0) / sps
    b = rolloff
    out = np.empty(length)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            out[i] = 1.0 - b + 4.0 * b / math.pi
        elif abs(abs(x) - 1.0 / (4.0 * b)) < 1e-9:
            out[i] = b / math.sqrt(2) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * b))
                                         + (1 - 2 / math.pi) * math.cos(math.pi / (4 * b)))
        else:
            out[i] = (math.sin(math.pi * x * (1 - b)) + 4 * b * x * math.cos(math.pi * x * (1 + b))) / (
                math.pi * x * (1 - (4 * b * x) ** 2))
    return out


class _Fir:
    """A real or complex FIR that streams: the last taps-1 inputs carry."""

    def __init__(self, taps: np.ndarray, dtype=np.float64):
        self.taps = taps
        self._hist = np.zeros(len(taps) - 1, dtype=dtype)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        joined = np.concatenate([self._hist, x])
        out = np.convolve(joined, self.taps, mode="valid")
        if len(self.taps) > 1:
            self._hist = joined[-(len(self.taps) - 1):]
        return out


class _Decimator:
    """Integer-factor decimation behind a windowed-sinc low-pass, computing
    only the outputs that are kept."""

    def __init__(self, factor: int, rate: float):
        self.factor = factor
        taps = 8 * factor + 1
        n = np.arange(taps) - (taps - 1) / 2.0
        cutoff = 0.45 * rate / factor          # 4.3 kHz at 48k/5: nothing in band
        self.h = (2.0 * cutoff / rate * np.sinc(2.0 * cutoff / rate * n)) * np.hamming(taps)
        self._hist = np.zeros(taps - 1)
        self._phase = 0                        # input samples to skip before the next output

    def __call__(self, x: np.ndarray) -> np.ndarray:
        joined = np.concatenate([self._hist, x])
        taps = len(self.h)
        first = taps - 1 + self._phase         # index in joined of the first output's newest sample
        count = (len(joined) - 1 - first) // self.factor + 1 if len(joined) > first else 0
        out = np.zeros(count)
        if count:
            for k in range(taps):
                start = first - k
                out += self.h[k] * joined[start:start + (count - 1) * self.factor + 1:self.factor]
            nxt = first + count * self.factor
        else:
            nxt = first
        # The next output's newest sample, counted from the start of the
        # next call's input.
        self._phase = nxt - len(joined)
        self._hist = joined[-(taps - 1):]
        return out


class _Agc:
    """Peak and valley followers on one tone's amplitude, a value per chunk
    (half a bit) - fast attack, slow decay - and the amplitude rescaled so the
    two sit at +-0.5."""

    def __init__(self, chunk: int, decay_bits: float, attack: float):
        self.chunk = chunk
        self.decay = 1.0 / (decay_bits * 2.0)
        self.attack = attack
        self.peak = 0.0
        self.valley = 0.0

    def __call__(self, amp: np.ndarray) -> np.ndarray:
        chunks = amp.reshape(-1, self.chunk)
        highs = chunks.max(axis=1).tolist()
        lows = chunks.min(axis=1).tolist()
        p, v, a, d = self.peak, self.valley, self.attack, self.decay
        peaks = []
        valleys = []
        for h, low in zip(highs, lows):
            p += (h - p) * (a if h > p else d)
            v += (low - v) * (a if low < v else d)
            peaks.append(p)
            valleys.append(v)
        self.peak, self.valley = p, v
        pk = np.repeat(np.array(peaks), self.chunk)
        vl = np.repeat(np.array(valleys), self.chunk)
        return (amp - 0.5 * (pk + vl)) / np.maximum(pk - vl, 1e-12)


class _Slicer:
    """One decision threshold: its bit clock, its framer, its FX.25 watcher."""

    def __init__(self, gain: float, sps: float):
        self.gain = gain
        self.inc = self.nominal = 1.0 / sps
        self.phase = 0.0
        self.level = 0
        self.nrzi_last = 0
        self.framer = hdlc.Framer()
        self.fx = fx25.Fx25Receiver()
        self.bits_seen = 0
        #: The last 32 transitions, one bit each: 1 where it fell where the
        #: clock expected (DCD_WIDTH).
        self.dcd_history = 0

    def clock(self, levels: np.ndarray) -> list[int]:
        """Sliced levels (one per sample) to the level at each bit's middle."""
        inc, inertia, fgain = self.inc, PLL_INERTIA, PLL_FREQ_GAIN
        lo, hi = self.nominal * (1 - PLL_FREQ_RANGE), self.nominal * (1 + PLL_FREQ_RANGE)
        phase, level = self.phase, self.level
        history, width, gap, mask = self.dcd_history, DCD_WIDTH, DCD_GAP_BITS, (1 << DCD_WINDOW) - 1
        joined = np.empty(len(levels) + 1, dtype=np.int8)
        joined[0] = level
        joined[1:] = levels
        changes = np.flatnonzero(joined[1:] != joined[:-1]).tolist()
        bits: list[int] = []
        last = -1
        for i in changes:
            run = i - last - 1
            if run * inc > gap:
                history = 0
            if run:
                total = phase + run * inc
                whole = int(total)
                if whole:
                    bits.extend([level] * whole)
                phase = total - whole
            # The transition sample: pull toward sampling half a bit after it.
            err = phase - 0.5
            history = ((history << 1) | (-width < err < width)) & mask
            if fgain:
                inc -= fgain * err * inc
                inc += PLL_FREQ_LEAK * (self.nominal - inc)
                inc = lo if inc < lo else hi if inc > hi else inc
            phase = 0.5 + err * inertia + inc
            level ^= 1
            if phase >= 1.0:
                phase -= 1.0
                bits.append(level)
            last = i
        run = len(levels) - last - 1
        if run * inc > gap:
            history = 0
        if run:
            total = phase + run * inc
            whole = int(total)
            if whole:
                bits.extend([level] * whole)
            phase = total - whole
        self.phase, self.level, self.inc = phase, level, inc
        self.dcd_history = history
        return bits


@dataclass
class Received:
    """One frame heard. `frame` is without its FCS; `sample` is where it
    started, counted in input samples since the receiver began; `fx25` is the
    correlation tag when FX.25 decoded it (None for plain AX.25) and
    `corrected` how many bytes its Reed-Solomon code put right."""
    frame: bytes
    sample: int
    fx25: int | None = None
    corrected: int = 0


class _Demod:
    """One front end (a prefilter or none, a tone low-pass) and its slicers."""

    def __init__(self, rate: float, prefilter: tuple[float, float] | None, decay_bits: float,
                 prefilter_bits: float | None = None, lowpass=None):
        self.rate = rate
        self.sps = rate / BAUD
        self.pre = None
        if prefilter:
            taps = int(round(self.sps * (prefilter_bits or PREFILTER_BITS))) | 1
            self.pre = _Fir(_bandpass(rate, prefilter[0], prefilter[1], taps))
        if lowpass:
            window = _rrc(self.sps, *lowpass)
        else:
            window = _hann(max(4, int(round(self.sps * WINDOW_BITS))))
        self.mark = _Fir(window.astype(np.complex128), np.complex128)
        self.space = _Fir(window.astype(np.complex128), np.complex128)
        self._w_mark = 2.0 * math.pi * MARK_HZ / rate
        self._w_space = 2.0 * math.pi * SPACE_HZ / rate
        self._n = 0
        chunk = max(1, int(round(self.sps / 2.0)))
        self.chunk = chunk
        self.agc_mark = _Agc(chunk, decay_bits, AGC_ATTACK)
        self.agc_space = _Agc(chunk, decay_bits, AGC_ATTACK)
        self.slicers = [_Slicer(g, self.sps) for g in GAINS]

    def __call__(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.pre is not None:
            x = self.pre(x)
        n = self._n + np.arange(len(x))
        self._n = (self._n + len(x)) % (int(self.rate) * 1000)
        m = np.abs(self.mark(x * np.exp(-1j * self._w_mark * n)))
        s = np.abs(self.space(x * np.exp(-1j * self._w_space * n)))
        return self.agc_mark(m), self.agc_space(s)


class Receiver:
    """Audio in, frames out, at any sample rate from 8 kHz up.

        rx = Receiver(48_000)
        for heard in rx.feed(pcm):           # s16le bytes, or float samples
            frame = ax25.parse(heard.frame)

    Frames come out once each, however many slicers decoded them, and FX.25
    and plain AX.25 copies of one transmission count once. Audio is processed
    in blocks of `block_ms` (100 ms: numpy's cost per call matters below that,
    and a frame takes longer than that to send anyway), so a frame is reported
    up to that long after its closing flag; `flush()` processes what is
    waiting.
    """

    def __init__(self, rate: int = 12_000, *, diversity: bool = True, block_ms: float = 100.0):
        if rate < 2.5 * SPACE_HZ:
            raise PacketError(f"{rate} Hz cannot carry a {SPACE_HZ:g} Hz tone")
        self.rate = rate
        self.baud = BAUD
        factor = max(1, int(rate // MIN_RATE))
        self.decimator = _Decimator(factor, rate) if factor > 1 else None
        self.factor = factor
        self.inner_rate = rate / factor
        specs = DEMODS if diversity else DEMODS[:1]
        self.demods = [_Demod(self.inner_rate, *spec) for spec in specs]
        chunk = self.demods[0].chunk
        # A block is a whole number of AGC chunks after decimation.
        inner = max(chunk, int(self.inner_rate * block_ms / 1000.0) // chunk * chunk)
        self.block = inner * factor
        self._pending = np.zeros(0)
        self._consumed = 0                    # input samples processed
        self._recent: list[tuple[bytes, int]] = []
        self._held: list[Received] = []
        #: Counters for a status line: frames passed, and how many came
        #: through FX.25 with corrections.
        self.frames = 0
        self.fx25_frames = 0
        #: Data carrier detect: True while some slicer's bit clock is locked
        #: to something that looks like AFSK (DCD_WIDTH above). Updated
        #: once a block, so it lags the air by up to a block: the engine runs
        #: its receiver with shorter blocks for that reason.
        self.dcd = False

    @property
    def slicers(self):
        return [s for d in self.demods for s in d.slicers]

    def feed(self, audio) -> list[Received]:
        if isinstance(audio, (bytes, bytearray, memoryview)):
            raw = bytes(audio)
            x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
        else:
            x = np.asarray(audio, dtype=np.float64)
        self._pending = np.concatenate([self._pending, x])
        out: list[Received] = []
        while len(self._pending) >= self.block:
            block, self._pending = self._pending[:self.block], self._pending[self.block:]
            out += self._process(block)
        return out

    def flush(self) -> list[Received]:
        """Process whatever is waiting, padded with silence to a block, and
        hand over anything held for an FX.25 block that never finished."""
        out = self.feed(np.zeros(self.block - len(self._pending))) if len(self._pending) else []
        for slicer in self.slicers:
            slicer.fx.reset()
        return out + self._dedupe([])

    def _process(self, block: np.ndarray) -> list[Received]:
        x = self.decimator(block) if self.decimator is not None else block
        start = self._consumed
        self._consumed += len(block)
        found: list[Received] = []
        spb = self.rate / self.baud            # input samples per bit
        for demod in self.demods:
            mark, space = demod(x)
            for slicer in demod.slicers:
                levels = (mark - slicer.gain * space > 0).astype(np.int8)
                bits = slicer.clock(levels)
                if not bits:
                    continue
                arr = np.array(bits, dtype=np.int8)
                prev = np.empty_like(arr)
                prev[0] = slicer.nrzi_last
                prev[1:] = arr[:-1]
                slicer.nrzi_last = int(arr[-1])
                data = (arr == prev).astype(np.int8)
                found += self._frame(slicer, data, start, len(block), spb)
        best = max(s.dcd_history.bit_count() for s in self.slicers)
        if best >= DCD_ON:
            self.dcd = True
        elif best <= DCD_OFF:
            self.dcd = False
        return self._dedupe(found)

    @staticmethod
    def _frame(slicer: _Slicer, data: np.ndarray, start: int, block_len: int, spb: float) -> list[Received]:
        """One slicer's NRZI-decoded bits for a block through its HDLC framer
        and its FX.25 receiver (shared with fsk.Receiver, whose bits come
        through a descrambler first)."""
        found: list[Received] = []
        n = len(data)
        # Date each bit within the block by its position: exact enough to
        # tell one slicer's copy of a frame from the next frame.
        feed = slicer.framer.feed_bit
        for i, bit in enumerate(data.tolist()):
            frame = feed(bit)
            if frame is not None:
                end = start + int((i + 1) / n * block_len)
                found.append(Received(frame, max(0, end - int((_line_bits(frame) + 8) * spb))))
        for got in slicer.fx.feed(data):
            begin = start + int((got.start_bit - slicer.bits_seen) / n * block_len)
            found.append(Received(got.frame, max(0, begin), got.tag, got.corrected))
        slicer.bits_seen += n
        return found

    def _dedupe(self, found: list[Received]) -> list[Received]:
        """Each frame once. Two decodes of one frame start within a few bits
        of each other; the same bytes sent twice start at least a frame apart,
        so "the same bytes starting within half a frame" is one frame.

        A plain decode is held back while any slicer is part-way through an
        FX.25 block, as direwolf does, because the FX.25 copy of the same frame
        finishes later (after the check bytes) and should be the one reported:
        it says the frame came through FX.25 and how much was corrected. That
        delays a frame by at most one block's check bytes and padding.
        """
        horizon = self._consumed - int(5 * self.rate)
        self._recent = [(f, s) for f, s in self._recent if s > horizon]
        busy = any(s.fx.busy for s in self.slicers)
        pool = sorted(self._held + found, key=lambda h: (h.fx25 is None, h.sample))
        self._held = []
        out = []
        for heard in pool:
            tolerance = int(len(heard.frame) * 8 * self.rate / self.baud / 2)

            def same(frame, sample):
                return frame == heard.frame and abs(sample - heard.sample) < tolerance

            if any(same(f, s) for f, s in self._recent):
                continue
            if heard.fx25 is None and busy:
                if not any(same(h.frame, h.sample) for h in self._held):
                    self._held.append(heard)
                continue
            self._recent.append((heard.frame, heard.sample))
            out.append(heard)
            self.frames += 1
            if heard.fx25 is not None:
                self.fx25_frames += 1
        return sorted(out, key=lambda h: h.sample)


def _line_bits(frame: bytes) -> int:
    """How many line bits a frame (without FCS) took, stuffing included."""
    return len(hdlc.stuff(hdlc.with_fcs(frame)))
