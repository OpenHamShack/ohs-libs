"""G3RUH-compatible FSK at 9600 baud and up, and 4-FSK: the fast modem.

1200 baud AFSK (afsk.py, demod.py) puts two audio tones through a voice
radio. G3RUH's 9600 baud modem (James Miller, 1988) does not use tones at
all: the data itself, shaped and scrambled, frequency-modulates the
carrier directly, so a symbol is a deviation (a one high, a zero low, or
the other way round) and the receiver's FM discriminator hands the data
straight back. Every 9600 baud station on the air - TNCs, Kenwood's data
radios, direwolf's `-B 9600` - is this modem; this module is our copy of
it, both ways, at any baud.

Between HDLC (hdlc.py: flags, stuffing, FCS, NRZI) and the radio it adds
two things, in this order on transmit and the reverse on receive:

**The scrambler, x^17 + x^12 + 1.** Each line level after NRZI is XORed
with the scrambler's output 12 and 17 bits back, the result fed back in
(`lfsr = lfsr << 1 | out`). It exists because a run of flags or of
stuffed ones is a near-constant frequency, which a radio's AC-coupled
audio path drifts away from and a receiver's bit clock has nothing to
lock to; scrambled, every pattern looks random, transitions come about
every other bit and the mean is zero. The descrambler is self-synchronising
(it XORs the *received* bits 12 and 17 back, no feedback of its own), so a
receiver locks within 17 bits wherever it joins, a bit error costs three
bits, and a receiver whose discriminator is wired backwards gets every
descrambled bit inverted - which NRZI then ignores, as it ignores any
inversion. Polarity therefore never matters, as with AFSK. The taps are
direwolf's (`demod_9600.h`, `gen_tone.c`): `out = in ^ lfsr>>16 ^ lfsr>>11`,
checked both ways against its gen_packets and atest
(tests/test_fsk_direwolf.py).

**The pulse shape.** A square NRZ waveform FM-modulated would splash its
sidebands across the neighbours; G3RUH filtered the baseband so a 9600
baud signal at +-3 kHz deviation fits a 25 kHz channel. Here each bit is a
Gaussian-filtered rectangle (BT 0.5, as GMSK's family; `shape="gaussian"`)
or a raised-cosine step, one bit long (`shape="rc"`, which is what
direwolf's own transmitter makes). Both reach full deviation at every bit's
centre after a change, so the discriminator's eye is open where the
receiver samples. Gaussian is the default because it is the cleaner
(`python -m shack_packet.fsk_bench --only occupied`, 18/10/2026): at 9600
baud and +-3 kHz, 99 % of the power within 10.35 kHz (10.6 for the
raised-cosine step) and the power beyond +-12.5 kHz, in the next 25 kHz
channel, 52 dB down (47 for the step). Both decode the same.

**Deviation and bandwidth scale with the baud** (DEVIATION_PER_BAUD, 3 kHz
at 9600, so the modulation index h = 2 x 3 / 9.6 = 0.625 at every speed):
19 200 baud is +-6 kHz, 38 400 is +-12 kHz, and the occupied (99 %)
bandwidth doubles with each: 10.4, 20.8 and 41.5 kHz (`occupied_hz`).
9600 fits a 25 kHz channel; 19 200 wants 50 kHz and 38 400 100 kHz, which
on 23 cm is a data segment, not a channel.

**The receiver** (`Receiver`, the same interface as demod.Receiver, which
the engine uses unchanged): the discriminator's output, at any rate of
four or more samples a bit, goes through a low-pass at 0.6 baud (the noise
outside the data's band is most of the noise there is), then a
peak and valley follower like demod's AGC, which takes out the DC
that a station's frequency error puts on a discriminator - a nano 3 kHz
off at 9600 baud is a DC shift as big as the deviation itself - and scales
what is left to +-0.5 (`_Level`, a value per eight bits). Then one slicer
at zero, with demod's second-order bit clock, the descrambler, NRZI, an
HDLC framer and an FX.25 receiver.

Measured on the IQ loopback bench at 9600 baud, 10-11 dB CNR, 120 frames
(18/10/2026): the low-pass at 0.45/0.5/0.55/0.6/0.65/0.75 baud decoded
41/68/74/74/65/40 (0.6 kept); three slicers at -0.12, 0 and +0.12 of the
swing decoded exactly what one did, so one runs (the slicers are the
per-bit Python); the AGC's decay over 200/400/800 bits was within noise.
The level follower (`_Level`) per eight bits instead of demod's per half
bit decoded more, not less: 9600 and 38 400 baud at 10-11 dB, on and 3 kHz
off frequency, 60 frames a cell, 38/57 per half bit (attack 0.5) against
41/65 per eight bits with attack 0.2 (0.15: 43/66, 0.3: 41/66, 0.5: 40/61,
0.8: 36/56), for a sixteenth of the loop.

**4-FSK** (`bits=2`; modem.py's 4fsk modes, OHS-to-OHS only): the same
bit stream - NRZI, the scrambler, HDLC - two bits a symbol, at one of four
levels (FOUR_LEVELS: M17's, Gray-coded), each a raised-cosine step one
symbol long (`shape="rc"`: zero ISI at every symbol's middle, which four
levels need far more than two; Gaussian BT 0.5 left the inner levels'
eye too closed to decode without noise once the receive filter was
added). No sync word or symbol framing: the receiver turns each symbol
back into its two bits in order and the descrambler and HDLC flags find
everything else, wherever a frame starts in a pair. The receiver is the
2-FSK one with three thresholds (-1/3, 0, +1/3 of the swing, after the
same level follower) and a symbol clock that only steers on crossings that
mark a boundary (_SymbolClock). Its low-pass is at 0.75 of the symbol
rate (RX_CUTOFF_4): at 0.6, 2-FSK's, the inner levels' eye closed; 0.75
and 0.9 decoded the same through noise and 1.1 lost 2-4 dB. It needs
about 5 dB more CNR than 2-FSK (fsk_bench), the usual price of four
levels to a discriminator.

Nothing here keys anything: the transmit side fills an array.
"""

from __future__ import annotations

import math

import numpy as np

from . import afsk, demod, hdlc
from .errors import PacketError

#: The two-level speeds this modem is built and tested for: G3RUH's three,
#: and the OHS-only 76 800 and 153 600 (modem.py's gfsk modes). Any baud
#: works if the rate gives SPS_MIN samples a bit; these are the ones the
#: engine offers. 4-FSK's are modes by name (modem.MODES).
BAUDS = (9600, 19_200, 38_400, 76_800, 153_600)
#: G3RUH's own, the ones other stations speak.
G3RUH_BAUDS = (9600, 19_200, 38_400)
#: G3RUH's +-3 kHz at 9600 baud, kept in proportion at other speeds.
DEVIATION_PER_BAUD = 3_000.0 / 9_600
#: 99 % power bandwidth (Hz, both sides) of the Gaussian shape at
#: DEVIATION_PER_BAUD, measured on the loopback bench (fsk_bench, occupied).
OCCUPIED_PER_BAUD = 10_400.0 / 9_600
#: Fewest samples a bit the receiver will work at: its bit clock places a
#: transition to the nearest sample, and below four that is a quarter bit.
SPS_MIN = 4
#: Gaussian BT, and how many bits each side of a bit its filter reaches.
GAUSSIAN_BT = 0.5
GAUSSIAN_SPAN_BITS = 2
#: The receive low-pass, as a fraction of the baud.
RX_CUTOFF = 0.6
#: 4-FSK's, as a fraction of the symbol rate (fsk_bench, below).
RX_CUTOFF_4 = 0.75
#: 4-FSK's symbol clock: how much of its phase it keeps at each crossing
#: (demod.PLL_INERTIA is 0.85).
CLOCK_INERTIA_4 = 0.85
#: The slicers' thresholds, as fractions of the AGC's +-0.5 swing.
THRESHOLDS = (0.0,)
#: The AGC's decay, in bits: a few frames, so the DC it follows (a
#: station's frequency error) is held through an over and let go between.
AGC_DECAY_BITS = 400.0
AGC_ATTACK = 0.2
AGC_CHUNK_BITS = 8
#: 4-FSK's symbol levels (of full deviation) by the pair of bits they
#: carry, first bit high: 00 +1/3, 01 +1, 10 -1/3, 11 -1. M17's mapping:
#: Gray, so the commonest error, a level mistaken for its neighbour, costs
#: one bit of the two.
FOUR_LEVELS = np.array([1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0])
#: The pair of bits for each received level, lowest first (-1, -1/3, +1/3, +1).
_PAIRS_BY_LEVEL = np.array([[1, 1], [1, 0], [0, 0], [0, 1]], dtype=np.int8)


def deviation_hz(baud: float) -> float:
    return DEVIATION_PER_BAUD * baud


def occupied_hz(baud: float) -> float:
    return OCCUPIED_PER_BAUD * baud


def _sps(rate: float, baud: float) -> float:
    sps = rate / baud
    if sps < SPS_MIN:
        raise PacketError(f"{rate:g} Hz is fewer than {SPS_MIN} samples a bit at {baud:g} baud")
    return sps


# -- the scrambler -------------------------------------------------------------------------


class Scrambler:
    """x^17 + x^12 + 1, multiplicative: line levels in, scrambled levels
    out, the state carried between calls (module docstring)."""

    def __init__(self):
        self.lfsr = 0

    def __call__(self, levels) -> list[int]:
        out = []
        lfsr = self.lfsr
        for b in levels:
            x = (b ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
            lfsr = ((lfsr << 1) | x) & 0x1FFFF
            out.append(x)
        self.lfsr = lfsr
        return out


class Descrambler:
    """The self-synchronising inverse, vectorised: each output is the input
    XOR the inputs 12 and 17 back. Keeps the last 17 inputs between calls."""

    def __init__(self):
        self._hist = np.zeros(17, dtype=np.int8)

    def __call__(self, bits: np.ndarray) -> np.ndarray:
        bits = np.asarray(bits, dtype=np.int8)
        if not len(bits):
            return bits
        joined = np.concatenate([self._hist, bits])
        n = len(bits)
        out = joined[17:] ^ joined[5:5 + n] ^ joined[0:n]
        self._hist = joined[-17:]
        return out


# -- transmit ------------------------------------------------------------------------------


def _gaussian_pulse(sps: int, bt: float = GAUSSIAN_BT, span: int = GAUSSIAN_SPAN_BITS) -> np.ndarray:
    """A one-bit rectangle through a Gaussian filter of bandwidth bt x baud:
    the frequency pulse of one bit, centred, odd length, reaching `span`
    bits and a half either side; scaled so a long run of one symbol sits at
    exactly full deviation."""
    half = int(round((span + 0.5) * sps))
    t = np.arange(-half, half + 1) / sps                                   # bit times from the bit's centre
    k = math.pi * bt * math.sqrt(2.0 / math.log(2.0))
    erf = np.vectorize(math.erf)
    p = 0.5 * (erf(k * (t + 0.5)) - erf(k * (t - 0.5)))                    # the Gaussian over the bit
    return p / p.sum() * sps


def _rc_pulse(sps: int) -> np.ndarray:
    """The raised-cosine step as a pulse: 0.5 + 0.5 cos(pi t / T) over two
    bits, so each change of symbol is half a cosine a bit long centred on
    the boundary, and a bit's neighbours are zero at its centre."""
    t = np.arange(-sps, sps + 1) / sps
    p = 0.5 + 0.5 * np.cos(math.pi * t)
    return p / p.sum() * sps


class Modulator:
    """Data bits in, shaped baseband out (float, +-amplitude at full
    deviation), at `rate` = a whole number of samples a bit. Stateful, so a
    transmission built in pieces has no seam: NRZI's level, the scrambler
    and the pulse filter's history all carry between calls."""

    def __init__(self, rate: int, baud: float = 9600, amplitude: float = 0.5, shape: str = "gaussian", *,
                 bt: float = GAUSSIAN_BT, bits: int = 1):
        if bits not in (1, 2):
            raise PacketError(f"{bits} bits a symbol: this modem sends one (2-FSK) or two (4-FSK)")
        sps = rate / baud
        if abs(sps - round(sps)) > 1e-9 or round(sps) < SPS_MIN:
            raise PacketError(f"{rate} Hz is not a whole number of samples a bit ({SPS_MIN} or more) "
                              f"at {baud:g} baud")
        self.rate = int(rate)
        self.baud = float(baud)
        self.sps = int(round(sps))
        self.amplitude = amplitude
        self.bits_per_symbol = bits
        self.bt = float(bt)
        if shape == "gaussian":
            pulse = _gaussian_pulse(self.sps, self.bt)
        elif shape == "rc":
            pulse = _rc_pulse(self.sps)
        else:
            raise PacketError(f"no pulse shape {shape!r}")
        self.shape = shape
        self._fir = demod._Fir(pulse)
        self._delay = (len(pulse) - 1) // 2
        self.scrambler = Scrambler()
        self.level = 0                     # NRZI
        self._odd: list[int] = []          # 4-FSK: a scrambled bit waiting for its pair

    def levels(self, levels) -> np.ndarray:
        """Line levels after NRZI (0/1) to baseband: scrambled, as impulses
        a symbol apart (+-1 for 2-FSK; for 4-FSK each pair of bits as one
        of FOUR_LEVELS, GRAY's mapping), through the pulse filter."""
        scrambled = self.scrambler(list(levels))
        if self.bits_per_symbol == 2:
            scrambled = self._odd + scrambled
            self._odd = scrambled[len(scrambled) // 2 * 2:]
            pairs = np.array(scrambled[:len(scrambled) // 2 * 2], dtype=np.int8).reshape(-1, 2)
            symbols = FOUR_LEVELS[pairs[:, 0] * 2 + pairs[:, 1]] if len(pairs) else np.zeros(0)
        else:
            symbols = np.array(scrambled, dtype=np.float64) * 2.0 - 1.0
        if not len(symbols):
            return np.zeros(0)
        x = np.zeros(len(symbols) * self.sps)
        x[::self.sps] = symbols
        return self._fir(x) * self.amplitude

    def bits(self, bits) -> np.ndarray:
        levels, self.level = hdlc.nrzi(bits, self.level)
        return self.levels(levels)

    def flush(self) -> np.ndarray:
        """The filter's tail: the last symbol's pulse, played out (a 4-FSK
        bit left without its pair goes with a 0 beside it)."""
        head = self.levels([]) if not self._odd else self._pad_odd()
        return np.concatenate([head, self._fir(np.zeros(self._delay + self.sps)) * self.amplitude])

    def _pad_odd(self) -> np.ndarray:
        pair, self._odd = self._odd + [0], []
        x = np.zeros(self.sps)
        x[0] = FOUR_LEVELS[pair[0] * 2 + pair[1]]
        return self._fir(x) * self.amplitude


def transmission_bits(frames, *, fx25_mode=None, txdelay_ms: float = 100.0, tail_ms: float = 10.0,
                      baud: float = 9600) -> list[int]:
    """TXDELAY of flags, the frames (FX.25 or plain, afsk.transmission_bits's
    rules), a tail of flags: the same bits AFSK sends, at this baud."""
    return afsk.transmission_bits(frames, fx25_mode=fx25_mode, txdelay_ms=txdelay_ms, tail_ms=tail_ms, baud=baud)


def modulate(frames, rate: int, *, baud: float = 9600, amplitude: float = 0.5, fx25_mode=None,
             txdelay_ms: float = 100.0, tail_ms: float = 10.0, shape: str = "gaussian", bt: float = GAUSSIAN_BT,
             bits: int = 1) -> np.ndarray:
    """Frames (without their FCS) to one transmission's baseband, float.
    The filter's tail is included, so the last flag ends at full deviation
    before the carrier would drop. `baud` is symbols a second; with `bits`
    2 (4-FSK) the bit rate, and so TXDELAY's flags, is twice it."""
    mod = Modulator(rate, baud, amplitude, shape, bt=bt, bits=bits)
    bits = transmission_bits(frames, fx25_mode=fx25_mode, txdelay_ms=txdelay_ms, tail_ms=tail_ms,
                             baud=baud * bits)
    # The filter delays everything by `_delay` samples; the first samples it
    # makes are its own ramp in, which is preamble anyway.
    return np.concatenate([mod.bits(bits), mod.flush()])


def fm_iq(baseband: np.ndarray, rate: float, deviation: float, level: float = 0.5,
          phase: float = 0.0) -> np.ndarray:
    """Baseband (level = full deviation) to complex FM at `rate`: what a
    radio's modulator does, for the tests and the bench."""
    step = 2.0 * math.pi * np.asarray(baseband) * (deviation / level) / rate
    return np.exp(1j * (phase + np.cumsum(step)))


# -- receive -------------------------------------------------------------------------------


class _Level:
    """Peak and valley followers, a value per AGC_CHUNK_BITS of samples -
    fast attack, slow decay - and the signal shifted and scaled so the two
    sit at +-0.5: demod._Agc's idea, which follows a value per half bit
    because AFSK's tone amplitudes move that fast. Here what it follows is
    a station's frequency error and deviation, which do not move within an
    over, and per half bit its Python loop was a third of the receiver's
    CPU at 38 400 baud (77 000 turns a second); per eight bits it is 4800,
    and scrambled data has both symbols in nearly every eight bits."""

    def __init__(self, chunk: int, decay_bits: float, attack: float, chunk_bits: float):
        self.chunk = chunk
        self.decay = min(1.0, chunk_bits / decay_bits)
        self.attack = attack
        self.peak = 0.0
        self.valley = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        chunks = x.reshape(-1, self.chunk)
        highs = chunks.max(axis=1).tolist()
        lows = chunks.min(axis=1).tolist()
        p, v, a, d = self.peak, self.valley, self.attack, self.decay
        peaks, valleys = [], []
        for h, low in zip(highs, lows):
            p += (h - p) * (a if h > p else d)
            v += (low - v) * (a if low < v else d)
            peaks.append(p)
            valleys.append(v)
        self.peak, self.valley = p, v
        pk = np.repeat(np.array(peaks), self.chunk)
        vl = np.repeat(np.array(valleys), self.chunk)
        return (x - 0.5 * (pk + vl)) / np.maximum(pk - vl, 1e-12)


class _Fsk:
    """The front end: low-pass, the level follower, and the slicers
    (demod._Slicer, with a descrambler each)."""

    def __init__(self, rate: float, baud: float, *, cutoff: float = RX_CUTOFF, slicers: bool = True):
        self.rate = rate
        self.sps = rate / baud
        taps = int(round(self.sps * 4)) | 1
        n = np.arange(taps) - (taps - 1) / 2.0
        fc = cutoff * baud / rate
        h = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hamming(taps)
        self.lowpass = demod._Fir(h / h.sum())
        self.chunk = max(1, int(round(self.sps * AGC_CHUNK_BITS)))
        self.agc = _Level(self.chunk, AGC_DECAY_BITS, AGC_ATTACK, self.chunk / self.sps)
        self.slicers = []
        for t in THRESHOLDS if slicers else ():
            s = demod._Slicer(t, self.sps)
            s.descrambler = Descrambler()
            self.slicers.append(s)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return self.agc(self.lowpass(x))


class Receiver(demod.Receiver):
    """Discriminator output in (s16le bytes or float, any rate of SPS_MIN
    samples a bit or more), frames out: demod.Receiver's interface, so the
    engine, the dedupe and the FX.25 hold work unchanged (module
    docstring)."""

    def __init__(self, rate: int, baud: float = 9600, *, block_ms: float = 20.0, diversity: bool = True,
                 bits: int = 1, cutoff: float | None = None):
        _sps(rate, baud)
        if bits not in (1, 2):
            raise PacketError(f"{bits} bits a symbol: this receiver takes one (2-FSK) or two (4-FSK)")
        self.rate = rate
        self.bits_per_symbol = bits
        #: Bits a second (demod.Receiver's `baud`, which dates frames and
        #: dedupes them by their length in bits); symbols a second times
        #: `bits`.
        self.baud = float(baud) * bits
        self.decimator = None
        self.factor = 1
        self.inner_rate = rate
        if bits == 2:
            self.demods = [_Fsk4(rate, baud, cutoff=RX_CUTOFF_4 if cutoff is None else cutoff)]
        else:
            self.demods = [_Fsk(rate, baud, cutoff=RX_CUTOFF if cutoff is None else cutoff)]
        chunk = self.demods[0].chunk
        self.block = max(chunk, int(rate * block_ms / 1000.0) // chunk * chunk)
        self._pending = np.zeros(0)
        self._consumed = 0
        self._recent = []
        self._held = []
        self.frames = 0
        self.fx25_frames = 0
        self.dcd = False

    def _process(self, block: np.ndarray) -> list:
        start = self._consumed
        self._consumed += len(block)
        found = []
        spb = self.rate / self.baud
        for front in self.demods:
            if self.bits_per_symbol == 2:
                slicer = front.slicers[0]
                arr = slicer.descrambler(_PAIRS_BY_LEVEL[front.symbols(block)].ravel())
                if len(arr):
                    found += self._nrzi_frame(slicer, arr, start, len(block), spb)
                continue
            x = front(block)
            for slicer in front.slicers:
                levels = (x > slicer.gain).astype(np.int8)
                bits = slicer.clock(levels)
                if not bits:
                    continue
                arr = slicer.descrambler(np.array(bits, dtype=np.int8))
                found += self._nrzi_frame(slicer, arr, start, len(block), spb)
        best = max(s.dcd_history.bit_count() for s in self.slicers)
        if best >= demod.DCD_ON:
            self.dcd = True
        elif best <= demod.DCD_OFF:
            self.dcd = False
        return self._dedupe(found)

    def _nrzi_frame(self, slicer, arr: np.ndarray, start: int, block_len: int, spb: float) -> list:
        """Descrambled line levels through NRZI to the framer."""
        prev = np.empty_like(arr)
        prev[0] = slicer.nrzi_last
        prev[1:] = arr[:-1]
        slicer.nrzi_last = int(arr[-1])
        data = (arr == prev).astype(np.int8)
        return self._frame(slicer, data, start, block_len, spb)


# -- 4-FSK ---------------------------------------------------------------------------------


class _SymbolClock:
    """demod._Slicer's bit clock, for 4-FSK: a second-order loop pulled
    toward sampling half a symbol after a transition, reading the level
    (0-3, lowest first) at each symbol's middle.

    Only a crossing that marks the symbol boundary steers it: one between
    two neighbouring levels across the threshold between them (-1/3 to
    +1/3 across zero, +1/3 to +1 across +2/3 of the swing...), or between
    the outer levels across zero. A jump of two levels (+1 to -1/3) crosses
    its thresholds a sixth of a symbol either side of the boundary, and a
    loop that re-centres on every crossing it sees was pulled by each.
    Which jump a crossing belongs to is read half a symbol either side of
    it. Measured through the IQ loopback at 76 800 baud, 16 dB CNR,
    symbols wrong (18/10/2026): sampled at the best fixed phase, 0.02-0.07 %;
    this clock steered by zero crossings only, 0.17 %; by the crossings
    above, 0.06-0.12 %. Its inertia at 0.92/0.96/0.98 instead of
    demod's 0.85 was no better."""

    def __init__(self, sps: float, inertia: float = None):
        self.slicer = demod._Slicer(0.0, sps)
        self.slicer.descrambler = Descrambler()
        self.inertia = CLOCK_INERTIA_4 if inertia is None else inertia
        self.half = max(1, int(round(sps / 2.0)))
        self._tail = np.zeros(self.half + 1, dtype=np.int8)       # the last levels of the block before

    def __call__(self, q: np.ndarray) -> np.ndarray:
        """q: the level index (0-3) per sample. Returns the level index at
        each symbol's middle."""
        s, h = self.slicer, self.half
        q = np.asarray(q, dtype=np.int8)
        n = len(q)
        if not n:
            return q
        joined = np.concatenate([self._tail, q])
        off = len(self._tail)
        self._tail = joined[-(h + 1):]
        idx = np.flatnonzero(joined[off:] != joined[off - 1:-1])          # sample i of this block changed
        j = idx + off
        before = joined[np.maximum(j - h, 0)]
        ahead = np.minimum(j + h - 1, len(joined) - 1)
        after = joined[ahead]
        lo, hi = np.minimum(before, after), np.maximum(before, after)
        prev, now = joined[j - 1], joined[j]
        across0 = (np.minimum(prev, now) <= 1) & (np.maximum(prev, now) >= 2)
        steer = (((lo == 1) & (hi == 2)) | ((lo == 0) & (hi == 3))) & across0
        steer |= (lo == 2) & (hi == 3) & (np.minimum(prev, now) == 2) & (np.maximum(prev, now) == 3)
        steer |= (lo == 0) & (hi == 1) & (np.minimum(prev, now) == 0) & (np.maximum(prev, now) == 1)
        steer &= j + h - 1 < len(joined)                                   # the end of a block: unknown, skip
        events = idx[steer].tolist()
        inc, inertia, fgain = s.inc, self.inertia, demod.PLL_FREQ_GAIN
        lo_inc, hi_inc = s.nominal * (1 - demod.PLL_FREQ_RANGE), s.nominal * (1 + demod.PLL_FREQ_RANGE)
        phase = s.phase
        history, width, gap = s.dcd_history, demod.DCD_WIDTH, demod.DCD_GAP_BITS
        mask = (1 << demod.DCD_WINDOW) - 1
        at: list[int] = []
        last = -1
        for i in events + [n]:
            run = i - last - 1
            if run * inc > gap:
                history = 0
            if run > 0:
                total = phase + run * inc
                whole = int(total)
                for m in range(1, whole + 1):
                    at.append(last + math.ceil((m - phase) / inc))
                phase = total - whole
            if i == n:
                break
            err = phase - 0.5
            history = ((history << 1) | (-width < err < width)) & mask
            if fgain:
                inc -= fgain * err * inc
                inc += demod.PLL_FREQ_LEAK * (s.nominal - inc)
                inc = lo_inc if inc < lo_inc else hi_inc if inc > hi_inc else inc
            phase = 0.5 + err * inertia + inc
            if phase >= 1.0:
                phase -= 1.0
                at.append(i)
            last = i
        s.phase, s.inc, s.dcd_history = phase, inc, history
        return q[np.clip(np.array(at, dtype=np.int64), 0, n - 1)]


class _Fsk4:
    """4-FSK's front end: _Fsk's low-pass and level follower (which puts
    the outer levels at +-0.5), the three decision thresholds between the
    four levels, and the symbol clock."""

    def __init__(self, rate: float, baud: float, cutoff: float = RX_CUTOFF):
        self.front = _Fsk(rate, baud, cutoff=cutoff, slicers=False)
        self.chunk = self.front.chunk
        self.clock = _SymbolClock(rate / baud)
        self.slicers = [self.clock.slicer]
        self.edges = np.array([-1.0 / 3.0, 0.0, 1.0 / 3.0])

    def symbols(self, block: np.ndarray) -> np.ndarray:
        x = self.front(block)
        return self.clock(np.searchsorted(self.edges, x).astype(np.int8))
