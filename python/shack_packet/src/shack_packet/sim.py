"""A simulated radio channel: N stations sharing one frequency, in
simulated time, for the tests and for trying the engine without a radio.

Two fidelities, because they answer different questions:

- **audio**: every transmission is the real modulator's audio, summed at
  each receiver with everything else on the air and with white noise at the
  SNR asked for, and decoded by the real receiver (with its real DCD). Two
  stations transmitting at once garble each other exactly as they would on
  the air. Costs about 2 % of a core per receiving station per second of
  simulated time, so minutes of traffic, not hours.
- **frames**: transmissions are frames with an airtime (computed from the
  bits the modulator would send). A receiver gets each frame at the end of
  its transmission unless another transmission it can hear overlapped it
  (a collision: both lost), it was itself transmitting (half duplex), or a
  random draw at `loss` says the frame was lost. DCD is "something audible
  is on the air", after `dcd_lag_s`. Thousands of times faster: this is
  what the 100 kB transfers run on.

Who hears whom is a matrix (`hear(a, b, False)` makes a hidden terminal),
every radio has a key-up time (`rise_s`: audio before the radio is up to
power is not transmitted, so a TXDELAY shorter than it loses the frame),
and there is a fixed propagation and processing `delay_s`.

`Simulation` advances the clock from one deadline to the next - each
engine's, and the medium's - so a 20-minute transfer runs in seconds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from . import afsk
from .channel import RadioLink


class SimClock:
    """Simulated time, in seconds. Engines take it as their clock."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class _Tx:
    radio: SimRadio
    start: float
    end: float
    frames: list[bytes] = field(default_factory=list)
    audio: np.ndarray | None = None
    delivered: bool = False
    #: Frames mode: when the last frame's last bit is on the air (`end` is
    #: when the carrier drops, `drain_s` later), the preamble's length,
    #: and who has been handed the frames.
    data_end: float | None = None
    txdelay_s: float = 0.0
    heard_by: set = field(default_factory=set)
    #: Each frame's mode, on an adaptive radio (adapt.py); else None.
    modes: list | None = None


@dataclass
class RadioTiming:
    """What a real radio link adds to the frames-mode medium, all in
    seconds and none by default (the medium as it always was). Written for
    the Pluto (radios/pluto.py), whose figures are in `PLUTO`; any link's
    `timing()` gives the same quantities.

    - `tx_lag_s`: from the channel deciding to transmit to the first bit
      on the air: the key's writes, modulating the over, the first push.
    - `drain_s`: carrier left up after the last bit (the unkey waits for
      the queue to play out, then the quiet write).
    - `rx_lag_s`: from a frame's last bit on the air to the engine having
      it: receive blocks, the kernel's queue, the receiver's own block.
    - `busy_tail_s`: how long this station still calls the channel busy
      after the carrier drops (a power detector averages, and sees it a
      block late).
    - `rx_resume_s`: after our own carrier drops, how long before we hear
      anything (a Pluto pauses receive during its overs). A transmission
      whose preamble has not given us `min_lock_s` of flags by then is lost
      to us.
    """
    tx_lag_s: float = 0.0
    drain_s: float = 0.0
    rx_lag_s: float = 0.0
    busy_tail_s: float = 0.0
    rx_resume_s: float = 0.0


#: A fast-mode PlutoLink, from the timing it reported on the air
#: (fastfsk_test, 38 400 baud, 18/10/2026): unkey_tail_s 0.044-0.053,
#: rx_after_unkey_s 0.081-0.088, 50 ms receive blocks; busy is channel
#: power averaged over BUSY_AVERAGE_S (0.1 s) of those blocks; the key, the
#: modulation of up to 17 frames and the first push took 0.1-0.2 s from the
#: last acknowledgement heard to the over's start in that log, of which the
#: busy tail is most.
PLUTO = RadioTiming(tx_lag_s=0.03, drain_s=0.05, rx_lag_s=0.075, busy_tail_s=0.15, rx_resume_s=0.085)


class SimRadio(RadioLink):
    """One station's radio on a Medium."""

    def __init__(self, medium: Medium, name: str, rise_s: float = 0.0, turnaround_s: float | None = None,
                 hears_itself: bool = False, timing: RadioTiming | None = None, modes: tuple | None = None):
        self.medium = medium
        self.name = name
        self.rate = medium.rate
        self.baud = medium.baud
        self.mode = medium.modem_mode
        self.carries_frames = medium.mode == "frames"
        self.rise_s = rise_s
        #: What the radio says its key-up time is (a real link measures it);
        #: by default the truth.
        self.turnaround_s = rise_s if turnaround_s is None else turnaround_s
        #: A full-duplex SDR (a Pluto) receives its own transmissions: in
        #: frames mode each of its own frames comes back to it at the end of
        #: the transmission, as the Pluto hands back its own over.
        self.hears_itself = hears_itself
        #: Frames mode: the radio's own delays (RadioTiming), none by default.
        self.timing = timing or RadioTiming()
        self._carrier_until = 0.0
        #: An adaptive radio (frames mode): its ladder, and the modes its
        #: receiver demodulates now (the engine says, `listen_modes`); a
        #: frame in any other mode is not heard.
        self.modes = tuple(modes) if modes else None
        if self.modes:
            from . import modem
            self.mode = self.modes[0]
            self.baud = modem.mode(self.mode).bitrate
        self.listen: set | None = None
        self.listen_changes = 0
        self.keyed = False
        self.keyed_at: float | None = None
        self.sink = None
        self._busy = False

    def key(self) -> None:
        self.keyed = True
        self.keyed_at = self.medium.clock.now

    def unkey(self) -> None:
        self.keyed = False

    def send_pcm(self, pcm: bytes) -> None:
        if not self.keyed:
            raise RuntimeError("audio sent to an unkeyed radio")
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        start = self.medium.clock.now + self.medium.delay_s
        cut = int(self.rise_s * self.rate)
        audio = audio.copy()
        audio[:cut] = 0.0                       # the radio is not up yet
        self.medium._add(_Tx(self, start, start + len(audio) / self.rate, audio=audio))

    def listen_modes(self, modes) -> None:
        self.listen = set(modes)
        self.listen_changes += 1

    def send_frames(self, frames, txdelay_s, airtime_s, modes=None) -> None:
        if not self.keyed:
            raise RuntimeError("frames sent to an unkeyed radio")
        start = self.medium.clock.now + self.medium.delay_s + self.timing.tx_lag_s
        # A radio's next over starts once the last has drained: the unkey
        # before it waits for that (radios/base.py), which simulated time
        # does not, and two of one station's overs overlapping would be a
        # collision at every receiver.
        start = max(start, self._carrier_until)
        # Frames mode cannot hear a short preamble; this is its model of
        # one: a receiver needs MIN_LOCK_S of flags after the radio is up.
        lost = txdelay_s - self.rise_s < self.medium.min_lock_s
        end = start + airtime_s + self.timing.drain_s
        self._carrier_until = end
        self.medium._add(_Tx(self, start, end, frames=[] if lost else list(frames),
                             data_end=start + airtime_s, txdelay_s=txdelay_s, modes=list(modes) if modes else None))


class Medium:
    """The shared frequency. See the module docstring."""

    def __init__(self, clock: SimClock, mode: str = "frames", *, rate: int = 12_000, loss: float = 0.0,
                 snr_db: float | None = 20.0, delay_s: float = 0.002, dcd_lag_s: float = 0.05,
                 chunk_s: float = 0.02, seed: int = 0, min_lock_s: float = 0.06, baud: int = 1200,
                 modem_mode: str | None = None, ber: float = 0.0, quality=None):
        if mode not in ("frames", "audio"):
            raise ValueError(mode)
        self.clock = clock
        self.mode = mode
        self.rate = rate
        #: The modem on the air (modem.py): 1200 AFSK, or G3RUH FSK with the
        #: audio as its baseband (rate then at least four samples a bit).
        self.baud = baud
        #: Or the modem by name (modem.MODES), for the 4-FSK modes; `baud` is
        #: then its bit rate. (`mode` here is the medium's frames or audio.)
        self.modem_mode = modem_mode
        if modem_mode is not None:
            from . import modem
            self.baud = modem.mode(modem_mode).bitrate
        self.loss = loss
        #: Frames mode: a bit error rate on top of `loss`, so a long frame is
        #: lost more often than a short one (1 - (1 - ber) ^ its bits), as
        #: on a real channel near its threshold - what decides how long a
        #: frame and how wide a window pay at the fast modes.
        self.ber = ber
        #: Frames mode, adaptive radios: (talker, listener, time) -> C/N0 in
        #: dB-Hz, each direction its own (asymmetry, fades). A frame in a
        #: mode is then heard with `frame_success`'s chance, and handed up
        #: with that C/N0 as its receiver would measure it (+-1 dB).
        self.quality = quality
        self.snr_db = snr_db
        self.delay_s = delay_s
        self.dcd_lag_s = dcd_lag_s
        self.chunk_s = chunk_s
        self.min_lock_s = min_lock_s
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.radios: list[SimRadio] = []
        self._deaf: set[tuple[str, str]] = set()
        self._air: list[_Tx] = []
        self._audio_at = clock.now               # audio delivered up to here
        #: Counters for the tests' reports.
        self.collisions = 0
        self.lost = 0
        self.unheard_mode = 0
        self.delivered = 0
        self.airtime_s = 0.0
        self._fades: dict[tuple[int, str], bool] = {}

    def radio(self, name: str, **kw) -> SimRadio:
        r = SimRadio(self, name, **kw)
        self.radios.append(r)
        return r

    def hear(self, a: str, b: str, hears: bool = True) -> None:
        """Whether a and b hear each other (both ways)."""
        for pair in ((a, b), (b, a)):
            (self._deaf.discard if hears else self._deaf.add)(pair)

    def hears(self, listener: SimRadio, talker: SimRadio) -> bool:
        return listener is not talker and (talker.name, listener.name) not in self._deaf

    def _add(self, tx: _Tx) -> None:
        self._air.append(tx)
        self.airtime_s += tx.end - tx.start

    # -- time -------------------------------------------------------------------

    def poll(self, now: float) -> float | None:
        if self.mode == "audio":
            return self._poll_audio(now)
        return self._poll_frames(now)

    def _poll_frames(self, now: float) -> float | None:
        for tx in self._air:
            if tx.delivered or tx.data_end is None or tx.data_end > now + 1e-9:
                continue
            waiting = False
            for r in self.radios:
                if r.name in tx.heard_by:
                    continue
                if tx.data_end + r.timing.rx_lag_s > now + 1e-9:
                    waiting = True
                    continue
                tx.heard_by.add(r.name)
                own = r is tx.radio and r.hears_itself
                if r.sink is None or not (own or self.hears(r, tx.radio)):
                    continue
                if not own and self._collided(tx, r):
                    self.collisions += 1
                    continue
                for n, frame in enumerate(tx.frames):
                    mode = tx.modes[n] if tx.modes else None
                    if mode is not None:
                        if r.listen is not None and mode not in r.listen:
                            self.lost += 1
                            self.unheard_mode += 1
                            continue
                        cn0 = None
                        if self.quality is not None:
                            # Each frame at its own time in the over: a fade
                            # that starts part-way through takes the rest.
                            at = tx.start + (tx.data_end - tx.start) * (n + 0.5) / len(tx.frames)
                            cn0 = self.quality(tx.radio.name, r.name, at)
                            if self.rng.random() >= frame_success(mode, cn0, len(frame)):
                                self.lost += 1
                                continue
                            cn0 += self.rng.gauss(0.0, 1.0)
                        self.delivered += 1
                        r.sink.frame_in(frame, mode=mode, cn0=cn0)
                        continue
                    if self.loss and self.rng.random() < self.loss:
                        self.lost += 1
                        continue
                    if self.ber and self.rng.random() < 1.0 - (1.0 - self.ber) ** ((len(frame) + 4) * 8):
                        self.lost += 1
                        continue
                    self.delivered += 1
                    r.sink.frame_in(frame)
            tx.delivered = not waiting
        # Keep finished transmissions a little while: a later-ending one may
        # still need to know it was overlapped.
        horizon = now - 30.0
        self._air = [t for t in self._air if t.end > horizon]
        upcoming = []
        for r in self.radios:
            busy = any(t.start + self.dcd_lag_s <= now + 1e-9 < t.end + r.timing.busy_tail_s
                       and self.hears(r, t.radio) for t in self._air)
            if busy != r._busy:
                r._busy = busy
                if r.sink is not None:
                    r.sink.busy_in(busy)
        for t in self._air:
            edges = [t.start + self.dcd_lag_s, t.end]
            edges += [t.end + r.timing.busy_tail_s for r in self.radios]
            if not t.delivered and t.data_end is not None:
                edges += [t.data_end + r.timing.rx_lag_s for r in self.radios]
            upcoming += [e for e in edges if e > now + 1e-9]
        return min(upcoming) if upcoming else None

    def _collided(self, tx: _Tx, listener: SimRadio) -> bool:
        for other in self._air:
            if other is tx:
                continue
            if other.radio is listener:
                # Our own over, and the receiver's pause after it: lost
                # unless we were listening again in time to lock on the
                # preamble (RadioTiming.rx_resume_s).
                # Without a timing the medium is as it always was: any
                # overlap with our own over loses the frame.
                if not listener.timing.rx_resume_s:
                    if other.start < tx.end and other.end > tx.start:
                        return True
                    continue
                ready = other.end + listener.timing.rx_resume_s
                if other.start < tx.end and ready > tx.start + tx.txdelay_s - self.min_lock_s + 1e-9:
                    return True
                continue
            if other.end <= tx.start or other.start >= tx.end:
                continue
            if self.hears(listener, other.radio):
                return True
        return False

    def _poll_audio(self, now: float) -> float:
        n = int(round(self.chunk_s * self.rate))
        while self._audio_at + self.chunk_s <= now + 1e-9:
            t0 = self._audio_at
            t1 = t0 + n / self.rate
            for r in self.radios:
                if r.sink is None:
                    continue
                chunk = np.zeros(n)
                if not any(t.radio is r and t.start < t1 and t.end > t0 for t in self._air):
                    for tx in self._air:
                        if tx.end <= t0 or tx.start >= t1 or not self.hears(r, tx.radio):
                            continue
                        if self._faded(tx, r):
                            continue
                        first = int(round((t0 - tx.start) * self.rate))
                        lo, hi = max(0, first), min(len(tx.audio), first + n)
                        if hi > lo:
                            chunk[lo - first:hi - first] += tx.audio[lo:hi]
                    if self.snr_db is not None:
                        # SNR against the modulator's level: a sine at
                        # amplitude A has power A^2 / 2.
                        sigma = (0.5 / np.sqrt(2.0)) / (10 ** (self.snr_db / 20.0))
                        chunk += self.np_rng.normal(0.0, sigma, n)
                r.sink.pcm_in(chunk)
            self._audio_at = t1
        self._air = [t for t in self._air if t.end > self._audio_at - 1.0]
        return self._audio_at + self.chunk_s

    def _faded(self, tx: _Tx, listener: SimRadio) -> bool:
        """In audio mode `loss` is the chance a whole transmission fades out
        at one receiver (noise does the rest)."""
        if not self.loss:
            return False
        key = (id(tx), listener.name)
        if key not in self._fades:
            self._fades[key] = self.rng.random() < self.loss
            if len(self._fades) > 10_000:
                self._fades.clear()
        return self._fades[key]


def frame_success(mode, cn0_dbhz: float, nbytes: int) -> float:
    """The chance a frame of `nbytes` in `mode` decodes at `cn0_dbhz`: a
    logistic in the CNR (in the mode's occupied bandwidth) fitted to the
    IQ loopback (fsk_bench, 18/10/2026, frames of about 1050 bits: 50 %
    at 10.2 dB for the two-level modes and 15.4 dB for 4-FSK, ~95 % at
    12 and 18), raised to the frame's length in those frames."""
    import math

    from . import modem
    m = modem.mode(mode)
    cnr = cn0_dbhz - 10.0 * math.log10(m.occupied_hz)
    c50, width = (15.4, 0.9) if m.bits == 2 else (10.2, 0.6)
    p = 1.0 / (1.0 + math.exp(-(cnr - c50) / width))
    return p ** (max(1, (nbytes + 4) * 8) / 1050.0)


class Simulation:
    """Runs engines and a medium on one simulated clock."""

    def __init__(self, clock: SimClock, medium: Medium, engines=()):
        self.clock = clock
        self.medium = medium
        self.engines = list(engines)

    def add(self, engine) -> None:
        self.engines.append(engine)

    def step(self) -> float | None:
        """Everything due now, then the clock moves to the next deadline.
        Input an engine took during the pass (a frame the medium delivered)
        wakes it, and it is polled again at the same instant, as the
        Runner's wake event does on the air."""
        now = self.clock.now
        nxt = None
        for _ in range(1000):
            for e in self.engines:
                e._wake_event.clear()
            due = [e.poll(now) for e in self.engines]
            due.append(self.medium.poll(now))
            due = [d for d in due if d is not None]
            nxt = min(due) if due else None
            woken = any(e._wake_event.is_set() for e in self.engines)
            if not woken and (nxt is None or nxt > now + 1e-9):
                break
        return nxt

    def run(self, seconds: float, until=None) -> bool:
        """Run for `seconds` of simulated time, or until `until()` is true
        (checked at every deadline). True if `until` was met."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            if until is not None and until():
                return True
            nxt = self.step()
            if until is not None and until():
                return True
            self.clock.now = min(end, max(nxt, self.clock.now + 1e-6) if nxt is not None else end)
        return until() if until is not None else False


def modulated_airtime(frames, txdelay_s: float = 0.3, txtail_s: float = 0.03) -> float:
    """Airtime of frames at 1200 baud, for the tests' arithmetic."""
    bits = afsk.transmission_bits(frames, txdelay_ms=txdelay_s * 1000, tail_ms=txtail_s * 1000)
    return len(bits) / afsk.BAUD
