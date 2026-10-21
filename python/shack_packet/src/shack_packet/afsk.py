"""Bell 202 AFSK, 1200 baud: bits to audio for a radio's data input.

The receive side is demod.py. This is the transmit side: a mark is 1200 Hz
and a space 2200 Hz, one bit every 1/1200 s, and the radio FM-modulates the
audio. Nothing here keys anything - it fills an array; the app hands that to
the radio under its TX lease.

**Phase-continuous**, because that is what every TNC sends and what every
receiver's filters expect: the tone changes frequency at a bit boundary
without jumping in phase. A jump sprays energy across the channel (a click
at every space-mark change) and costs the receiver at the far end.

**Any sample rate.** 44.1 kHz is 36.75 samples a bit, so bits cannot all be
the same length in samples. The bit clock is kept as a running fraction and
each bit ends at the sample nearest its true time, so the long-run rate is
exact and no bit is more than one sample long or short - which a receiver's
clock recovery does not notice. 8, 12, 16, 22.05, 24, 44.1 and 48 kHz are all
tested; below about 5.5 kHz the space tone is not representable (Nyquist).
"""

from __future__ import annotations

import numpy as np

from . import fx25, hdlc
from .errors import PacketError

#: Bell 202, as used on 144.800 and everywhere else VHF packet is worked.
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
BAUD = 1200.0


class Modulator:
    """Line levels in, audio out. Stateful so a transmission can be built in
    pieces without a seam: the tone's phase and the fraction of a sample the
    bit clock is carrying both survive between calls."""

    def __init__(self, rate: int = 48_000, amplitude: float = 0.5,
                 baud: float = BAUD, mark: float = MARK_HZ, space: float = SPACE_HZ):
        if rate < 2.5 * space:
            raise PacketError(f"{rate} Hz cannot carry a {space:g} Hz tone")
        self.rate = rate
        self.amplitude = amplitude
        self.baud = baud
        self.mark = mark
        self.space = space
        self._phase = 0.0          # radians, of the tone
        self._clock = 0.0          # samples owed to the bit stream, fractional
        self.level = 0             # the NRZI line level, for frames in pieces

    def levels(self, levels) -> np.ndarray:
        """Audio for these line levels (0 mark, 1 space), float in
        [-amplitude, amplitude]."""
        levels = np.asarray(levels, dtype=np.int8)
        if not len(levels):
            return np.zeros(0)
        sps = self.rate / self.baud
        # Where each bit ends, in samples from the start of this call,
        # counting the fraction the previous call left over.
        ends = np.floor(self._clock + sps * np.arange(1, len(levels) + 1) + 1e-9).astype(np.int64)
        counts = np.diff(np.concatenate(([0], ends)))
        self._clock = (self._clock + sps * len(levels)) - ends[-1]
        freq = np.repeat(np.where(levels > 0, self.space, self.mark), counts)
        step = 2.0 * np.pi * freq / self.rate
        phase = self._phase + np.cumsum(step)
        out = np.sin(phase - step) * self.amplitude
        self._phase = float(phase[-1] % (2.0 * np.pi)) if len(phase) else self._phase
        return out

    def bits(self, bits) -> np.ndarray:
        """Audio for data bits: NRZI applied here, continuing the line level
        left by the last call."""
        levels, self.level = hdlc.nrzi(bits, self.level)
        return self.levels(levels)


def flags_for(ms: float, baud: float = BAUD) -> int:
    """How many flags fill `ms` milliseconds at `baud`, rounded up."""
    return max(0, int(np.ceil(ms / 1000.0 * baud / 8.0)))


def transmission_bits(frames, *, fx25_mode=None, txdelay_ms: float = 300.0,
                      tail_ms: float = 30.0, baud: float = BAUD) -> list[int]:
    """The data bits of one transmission: TXDELAY of flags, each frame, a
    tail of flags.

    `fx25_mode` is None for plain AX.25, "auto" for FX.25 with the code
    chosen by size as direwolf chooses it, or 16, 32 or 64 check bytes. A
    frame too long for any FX.25 block goes as plain AX.25 - the FX.25
    station loses its correction on that one frame and nobody loses the
    frame.

    TXDELAY is the radio's business as much as ours: it is how long the
    receiver at the far end needs for its squelch to open and its clock to
    lock, plus how long this radio takes to come up to power after PTT. 300 ms
    is the conventional default; a fast data radio manages with 100-150.
    The tail keeps the carrier up while the last bits leave the audio path:
    some sound cards and radios lose the final few milliseconds.
    """
    out = list(hdlc.FLAG_BITS) * max(1, flags_for(txdelay_ms, baud))
    for frame in frames:
        coded = None
        if fx25_mode is not None:
            coded = fx25.frame_bits(frame, check=None if fx25_mode == "auto" else int(fx25_mode))
        if coded is not None:
            # An FX.25 block carries its own flags inside; the flag before
            # the next tag gives a plain receiver's framer a clean edge.
            out += coded + list(hdlc.FLAG_BITS)
        else:
            out += hdlc.frame_bits(frame, flags_before=0, flags_after=1)
    out += list(hdlc.FLAG_BITS) * max(1, flags_for(tail_ms, baud))
    return out


def modulate(frames, rate: int = 48_000, *, amplitude: float = 0.5, fx25_mode=None,
             txdelay_ms: float = 300.0, tail_ms: float = 30.0) -> np.ndarray:
    """Frames (without their FCS) to the audio of one transmission, float.
    `pcm16` turns it into what a sound card or a radio's USB codec takes."""
    bits = transmission_bits(frames, fx25_mode=fx25_mode, txdelay_ms=txdelay_ms, tail_ms=tail_ms)
    return Modulator(rate, amplitude).bits(bits)


def pcm16(audio: np.ndarray) -> bytes:
    """Float audio in [-1, 1] as signed 16-bit little-endian samples."""
    return (np.clip(audio, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
