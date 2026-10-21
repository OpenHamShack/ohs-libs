"""What every IQ source is: a receiver tuned somewhere, handing out blocks.

One interface for an RTL-SDR on USB, an rtl_tcp server on the network, a
Pluto's receive side, a recording and the synthetic band the tests use, so
everything after `read()` - the channeliser, the receivers, the monitor -
runs the same whichever it is (QO-100 NB's FileDevice made the same choice
for the same reason: a recording stands in for the radio behind the radio's
own interface).

**Pull, not push.** `read()` blocks until the next block is in. Every
backend has a blocking read at its heart (librtlsdr's read_sync, a socket,
libiio's refill, a file), and a pull keeps each backend's threading to
none: the monitor's runner (sdr/runner.py) owns the one reader thread and
the queue behind it, and is where falling behind turns into dropped blocks.

**Samples are numbered, and the numbers count what was lost.** A block
carries `index`, the number of its first sample since the source opened,
and `dropped`, how many samples went missing just before it. So a gap in
the stream is a jump in `index`, and a frame's time (index / rate from the
source's first sample) stays right across it. What a backend can know about
its own losses differs: a file and the synthetic source know exactly;
librtlsdr, rtl_tcp and libiio say nothing when the USB host or the network
falls behind, so the live backends compare the samples delivered with the
wall clock (`LAG_LIMIT_S`) and count what is missing past that limit.

**Full scale is 1.0**: an 8-bit RTL's 127 and a Pluto's 12-bit 2047 both
arrive as 1.0, so levels in dBFS mean the same from every source (0 dBFS a
full-scale complex tone).

**Receive only, by construction.** Nothing here has a method that
transmits, and a backend that talks to transceiving hardware (the Pluto)
must hold its transmitter quiet itself (sdr/pluto.py).

Failures the operator must hear about are `SdrError(reason, detail)` with a
key from REASONS, which the app translates (as radios.REASONS); `detail` is
the library's or the device's own words, for a log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..errors import PacketError

#: Why a source would not open or stopped, as keys the app translates.
REASONS = {
    "no-library": "the library this receiver needs is not installed",
    "no-device": "no receiver was found",
    "busy": "the receiver is in use by another program",
    "open-failed": "the receiver could not be opened",
    "setup-failed": "the receiver would not take a setting",
    "rate-unsupported": "the receiver cannot run at that sample rate",
    "out-of-range": "the receiver cannot tune that frequency",
    "read-failed": "the receiver stopped sending samples",
    "disconnected": "the receiver went away",
    "not-receiver": "that address is not a receiver of this kind",
    "tx-not-quiet": "the transceiver's transmitter could not be confirmed off, so it was not used",
    "bad-file": "the recording cannot be played",
    "ended": "the recording has ended",
}

#: A live source more than this far behind the wall clock has lost samples
#: (librtlsdr, rtl_tcp and libiio overflow silently). Two seconds, because
#: a network or a busy USB host can deliver a second late and then catch up
#: in a burst without losing anything; past this, it did not.
LAG_LIMIT_S = 2.0


class SdrError(PacketError):
    """A source would not open or stopped. `reason` is a REASONS key."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class IqBlock:
    """Complex64 samples at full scale 1.0. `index` is the first sample's
    number since the source opened (counting lost ones), `time` its UTC in
    seconds, `dropped` how many samples were lost just before it."""
    samples: np.ndarray
    index: int
    time: float
    dropped: int = 0


@dataclass
class SourceStats:
    blocks: int = 0
    samples: int = 0
    #: Samples lost, and the number of separate losses.
    dropped: int = 0
    drops: int = 0
    #: How far behind the wall clock the stream is now, seconds (live only).
    lag_s: float = 0.0
    notes: list[str] = field(default_factory=list)


class IqSource:
    """The interface. Subclasses implement `_open`, `_read` (complex64 at
    full scale, or None at the end) and `_close`; this class numbers and
    dates the blocks and keeps the statistics.

    `centre` is the tuned frequency in Hz as asked for; the error of the
    receiver's crystal is the channeliser's to correct (sdr/channeliser.py),
    not the source's, so every source is told the frequency it was asked
    for. `gain` is dB, or None for the receiver's automatic gain."""

    kind = "source"
    #: Whether this source paces itself on real hardware (and so can fall
    #: behind the wall clock and lose samples unseen). Files and the
    #: synthetic band can run faster than real time and lose nothing.
    live = True

    def __init__(self, centre: float, rate: float, gain: float | None = None):
        self.centre = float(centre)
        self.rate = float(rate)
        self.gain = gain
        self.stats = SourceStats()
        self.opened = False
        self._next = 0                 # the next sample's index
        self._t0: float | None = None  # UTC of sample 0
        self._mono0 = 0.0              # monotonic clock when sample 0 arrived
        self._lost = 0                 # samples to report as dropped with the next block

    # -- what subclasses implement -------------------------------------------

    def _open(self) -> None:
        raise NotImplementedError

    def _read(self) -> np.ndarray | None:
        raise NotImplementedError

    def _close(self) -> None:
        pass

    # -- the interface -------------------------------------------------------

    def open(self) -> IqSource:
        if not self.opened:
            self._open()
            self.opened = True
        return self

    def close(self) -> None:
        if self.opened:
            self.opened = False
            self._close()

    def __enter__(self) -> IqSource:
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def lose(self, samples: int) -> None:
        """Count `samples` as lost before the next block (a backend that
        knows; the runner, when it had to drop a block)."""
        if samples > 0:
            self._lost += int(samples)

    def read(self) -> IqBlock | None:
        """The next block; None when a finite source has ended. Raises
        SdrError when a live one fails."""
        if not self.opened:
            raise PacketError("read() before open()")
        z = self._read()
        if z is None:
            return None
        now = time.monotonic()
        if self._t0 is None:
            # Sample 0 arrived a block's length before this one returned.
            self._t0 = time.time() - len(z) / self.rate
            self._mono0 = now - len(z) / self.rate
        elif self.live:
            # Missing samples show as the stream falling behind the clock;
            # past LAG_LIMIT_S the excess is lost, not late.
            due = (now - self._mono0) * self.rate
            lag = (due - (self._next + self._lost + len(z))) / self.rate
            if lag > LAG_LIMIT_S:
                self.lose(int((lag - 0.5 * LAG_LIMIT_S) * self.rate))
                lag = 0.5 * LAG_LIMIT_S
            self.stats.lag_s = max(0.0, lag)
        return self._block(z)

    def _block(self, z: np.ndarray) -> IqBlock:
        lost, self._lost = self._lost, 0
        if lost:
            self.stats.dropped += lost
            self.stats.drops += 1
        index = self._next + lost
        self._next = index + len(z)
        self.stats.blocks += 1
        self.stats.samples += len(z)
        return IqBlock(z, index, self._t0 + index / self.rate, lost)

    def status(self) -> dict:
        return {"kind": self.kind, "centre": self.centre, "rate": self.rate, "gain": self.gain,
                "open": self.opened, "blocks": self.stats.blocks, "samples": self.stats.samples,
                "dropped": self.stats.dropped, "drops": self.stats.drops, "lagS": round(self.stats.lag_s, 3),
                "notes": list(self.stats.notes)}


def cu8_to_complex(raw: bytes | np.ndarray) -> np.ndarray:
    """RTL-SDR samples (unsigned 8-bit I then Q, 127.5 the middle) to
    complex64 at full scale 1.0."""
    b = np.frombuffer(raw, np.uint8) if isinstance(raw, (bytes, bytearray, memoryview)) else raw
    b = b[: len(b) // 2 * 2]
    f = (b.astype(np.float32) - 127.5) * (1.0 / 127.5)
    return f.view(np.complex64)


def complex_to_cu8(z: np.ndarray) -> bytes:
    """The other way, for the fake rtl_tcp server and test recordings:
    clipped at full scale, as the RTL's ADC would."""
    f = np.empty(2 * len(z), np.float32)
    f[0::2] = z.real
    f[1::2] = z.imag
    return np.clip(np.rint(f * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()
