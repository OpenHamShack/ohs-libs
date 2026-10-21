"""An SDR's receiver as a packet radio's: one FM channel out of an IQ
stream, as 12 kHz audio and a busy flag - and `SdrRxLink`, a station's
radio that only listens.

**FmChannelReceiver** is PlutoLink's receiver, taken out of it so the
receive-only SDRs hear exactly as the Pluto does (one receiver, one set of
numbers): sdr.Channeliser (the SDR monitor's FFT filter bank) picks the
channel out, decimates to its channel rate and FM-demodulates; a small
rational resampler takes that to 12 kHz (19.2 kHz -> 12 kHz is 5/8: 201
taps at 96 kHz, flat to 4 kHz, 42 dB down at 6 kHz where it would fold).
3 kHz deviation reads as half full scale, the engine's own transmit level.
Busy is the channel's power more than BUSY_DB above its own floor (the
10th percentile of the last FLOOR_S), averaged over BUSY_AVERAGE_S - on
the real Pluto's empty channel a single 50 ms block crossed BUSY_DB about
once a second (18/10/2026), a busy the engine's CSMA would have waited out.

**SdrRxLink** (PLAN-0.2 §3.6.5) puts that receiver on any `sdr.IqSource` -
an RTL-SDR (librtlsdr), an rtl_tcp server, an SDRplay through the API the
user installed (sdr.sdrplay), a recording - and gives the engine a radio
that monitors and decodes and never transmits: `key()` is refused with
"receive-only" (an existing REASON the app already words), before any radio
call, by the same refusal check that runs every tick. The source is tuned
`ifo_hz` below the channel, so a receiver's DC spike and LO leakage sit
outside it, as PlutoLink's LOs do. Busy comes from the channel's own power,
so CSMA timing, the heard list and the monitor all work.

A source that fails mid-stream (unplugged, the network gone) is reported
once as a link event ("radio-error", what: "receive") and the link stays
open and silent, as PlutoLink's receiver does: the app says so and the
operator reopens it.
"""

from __future__ import annotations

import collections
import logging
import threading
from fractions import Fraction

import numpy as np

from ..demod import _Fir
from ..sdr.channeliser import Channeliser, _kaiser_lowpass
from ..sdr.plan import Channel
from ..sdr.source import IqSource, SdrError
from .base import AudioRadioLink, LinkRefused

log = logging.getLogger(__name__)

AUDIO_RATE = 12_000
RX_LEVEL = 0.5                   # 3 kHz deviation received = this of full scale
BUSY_DB = 8.0
#: Busy and level_db average the channel's power over this long.
BUSY_AVERAGE_S = 0.1
#: The fast modes' (FskChannelReceiver) average as many of the channel's
#: samples as BUSY_AVERAGE_S does of a 12.5 kHz one (BUSY_SAMPLES), and
#: no less than FSK_BUSY_AVERAGE_S: 100 ms at 9600 baud, 30 ms at 38 400,
#: 20 ms from 76 800. Busy is how long a station waits after the other's
#: carrier drops before it may answer, and at 38 400 baud and up, where an
#: over of acknowledgements is a few milliseconds of bits, 100 ms of it was
#: most of the turnaround (README, throughput).
BUSY_SAMPLES = 1_250
FSK_BUSY_AVERAGE_S = 0.02
#: How far back the floor (the 10th percentile of the blocks' power) looks.
#: Left as it was after the GM0OTB run (19/10/2026), whose busy held for a
#: minute on an empty channel, because this cannot do that. Simulated
#: noise through FmChannelReceiver at 2.4 MS/s, 70 s a case: flat noise,
#: never busy; a burst of interference, busy for the burst; clicks (a 2 ms
#: one 20 dB up in 30 % of blocks), 0.1 s at most; but the floor stepping
#: up 10 or 20 dB (an AGC giving back gain, the Pluto's slow_attack after a
#: strong signal anywhere in its capture), busy 8.9 s - until the old
#: blocks are under a tenth of the window, 0.9 x FLOOR_S - and an AGC-like
#: recovery of 30 dB (time constant 5 s) 11.7 s. Only a floor rising 1 dB/s
#: without a stop held it longer (32 s of a 40 s ramp). A step is exactly
#: what a station's carrier looks like, so no change of this level-only
#: detector shortens one without equally shortening how long a real
#: carrier is believed (it is forgotten after 0.9 x FLOOR_S too):
#: hysteresis or needing the energy for longer changes neither, and a
#: longer window lengthens both. The one way found to hold busy for that
#: minute was the engine's DCD kept from a mode no longer listened to
#: (engine.Engine.mode_in, fixed), and whatever else may yet hold busy is
#: capped by channel.MAX_BUSY_WAIT_S.
FLOOR_S = 10.0
#: How far back a receiver's peak level looks (FmChannelReceiver.peak_level_db).
PEAK_S = 0.3


class _Resampler:
    """A streaming rational resampler (up, filter, keep every down-th),
    for the channeliser's channel rate to the engine's 12 kHz."""

    def __init__(self, up: int, down: int, rate_in: float, cutoff: float):
        self.up, self.down = up, down
        h = _kaiser_lowpass(40 * up + 1, cutoff, rate_in * up, 6.5) * up
        self._fir = _Fir(h)
        self._at = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        z = np.zeros(len(x) * self.up)
        z[::self.up] = x
        y = self._fir(z)
        out = y[self._at::self.down]
        self._at = (self._at - len(y)) % self.down
        return out


class FmChannelReceiver:
    """IQ at `rate_iq` around `centre_hz` in; one channel's 12 kHz s16le
    audio out, and its level and busy (module docstring)."""

    def __init__(self, rate_iq: float, centre_hz: float, channel_hz: float, *, deviation_hz: float = 3_000.0,
                 deemphasis_us: float | None = None, ppm: float = 0.0):
        self.chan = Channeliser(rate_iq, centre_hz, [Channel("link", channel_hz)], deemphasis_us=deemphasis_us,
                                deviation_hz=deviation_hz, ppm=ppm)
        ratio = Fraction(AUDIO_RATE, int(round(self.chan.ch_rate)))
        if ratio.numerator > 16:
            raise SdrError("rate-unsupported", f"no small ratio from {self.chan.ch_rate} to {AUDIO_RATE} Hz")
        self._resample = _Resampler(ratio.numerator, ratio.denominator, self.chan.ch_rate, 0.43 * AUDIO_RATE)
        per_s = self.chan.ch_rate / self.chan.per_block
        self._powers: collections.deque = collections.deque(maxlen=max(10, int(FLOOR_S * per_s)))
        self._busy_blocks = max(1, round(BUSY_AVERAGE_S * per_s))
        #: The channel's power over its own floor (dB), None until the floor is known.
        self.level_db: float | None = None
        self.busy: bool | None = None
        #: The filter's noise bandwidth (Hz), roughly: its passband and half
        #: its skirt each side. For a level in dB over the floor to become a
        #: C/N0 (adapt.cn0_from_level).
        self.noise_bw_hz = self.chan.pass_hz + self.chan.stop_hz
        self._recent_levels: collections.deque = collections.deque(maxlen=max(1, int(PEAK_S * per_s)))

    def hear(self, z: np.ndarray, index: int | None = None) -> bytes:
        chunk = self.chan.feed(z, index)
        audio = self._resample(chunk.audio[0]) if chunk.audio.shape[1] else np.zeros(0)
        self._track(chunk)
        if self.level_db is not None:
            self._recent_levels.append(self.level_db)
        if not len(audio):
            return b""
        return (np.clip(audio * RX_LEVEL, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()

    def _track(self, chunk) -> None:
        """The channel's power against its floor: level_db and busy."""
        for p in chunk.power[0]:
            self._powers.append(float(p))
        if len(self._powers) >= self._powers.maxlen // 10 and len(chunk.power[0]):
            powers = np.fromiter(self._powers, float)
            floor = float(np.percentile(powers, 10))
            recent = float(powers[-self._busy_blocks:].mean())
            self.level_db = 10 * np.log10(max(recent, 1e-15) / max(floor, 1e-15))
            self.busy = bool(self.level_db > BUSY_DB)

    @property
    def peak_level_db(self) -> float | None:
        """The highest level of the last PEAK_S: what a frame that has just
        been decoded was heard at, its carrier having perhaps dropped since."""
        return max(self._recent_levels) if self._recent_levels else None


class FskChannelReceiver(FmChannelReceiver):
    """The fast FSK modes' receiver (fsk.py): one channel out of the IQ at
    the modem's own rate, `sps` samples a bit, with no resampler - the
    channeliser's decimation lands on it exactly, which is why a fast-mode
    Pluto runs at a rate that is a whole multiple of the baud
    (radios.pluto.FSK_RATE_IQ). Out comes the discriminator itself as
    float, +-RX_LEVEL at full deviation, for fsk.Receiver.

    The channel filter is flat to half the occupied bandwidth plus
    `offset_hz` and falls over another quarter of the baud. The margin is
    for a station off frequency (a Pluto whose crystal correction is wrong,
    as the nano's was by 19 ppm): fsk.Receiver's AGC takes the DC that an
    offset leaves on the discriminator, but a filter that cuts one of its
    sidebands it cannot mend. Wider is more noise, and FM near its
    threshold is all about the noise before the discriminator. Measured on
    the loopback bench at 9600 baud, 10-11 dB CNR, 60 frames a cell
    (18/10/2026), frames decoded on frequency / 3 kHz off: margin 1.5 kHz
    26/15, 2.7 kHz 24/24, 4 kHz 18/18. At 38 400 baud the margin is small
    against the channel and made no difference. An AFC (retuning the
    channel to the power-weighted frequency of each block) was tried and
    lost more on frequency than it gained off it: at that CNR its estimates
    wander by hundreds of hertz a block."""

    def __init__(self, rate_iq: float, centre_hz: float, channel_hz: float, *, baud: float, sps: int = 8,
                 deviation_hz: float | None = None, offset_hz: float = 2_700.0, ppm: float = 0.0,
                 occupied_hz: float | None = None):
        from .. import fsk
        r = int(round(rate_iq))
        if r % int(baud * sps):
            raise SdrError("rate-unsupported", f"{r} S/s is not a whole multiple of {baud:g} baud x {sps}")
        dev = fsk.deviation_hz(baud) if deviation_hz is None else float(deviation_hz)
        pass_hz = (fsk.occupied_hz(baud) if occupied_hz is None else float(occupied_hz)) / 2.0 + offset_hz
        self.chan = Channeliser(rate_iq, centre_hz, [Channel("link", channel_hz)], deviation_hz=dev, ppm=ppm,
                                decimation=r // int(baud * sps), pass_hz=pass_hz, stop_hz=pass_hz + 0.25 * baud)
        self.rate = int(self.chan.ch_rate)
        per_s = self.chan.ch_rate / self.chan.per_block
        self._powers = collections.deque(maxlen=max(10, int(FLOOR_S * per_s)))
        occupied = 2.0 * (pass_hz - offset_hz)
        #: How long busy averages the channel's power over (seconds).
        self.busy_average_s = min(BUSY_AVERAGE_S, max(FSK_BUSY_AVERAGE_S, BUSY_SAMPLES / occupied))
        self._busy_blocks = max(1, round(self.busy_average_s * per_s))
        self.level_db = None
        self.busy = None
        self.noise_bw_hz = self.chan.pass_hz + self.chan.stop_hz
        self._recent_levels: collections.deque = collections.deque(maxlen=max(1, int(PEAK_S * per_s)))
        #: The last busy stretch's frequency error (Hz, the station against
        #: the channel, power-weighted), or None: what a keyed test reads to
        #: see how far apart two Plutos' crystals are.
        self.freq_hz: float | None = None

    def hear(self, z: np.ndarray, index: int | None = None) -> np.ndarray:
        chunk = self.chan.feed(z, index)
        self._track(chunk)
        if self.busy and chunk.corr.shape[1]:
            self.freq_hz = round(self.chan.freq_hz(complex(chunk.corr[0].sum())), 1)
        if self.level_db is not None:
            self._recent_levels.append(self.level_db)
        return chunk.audio[0] * RX_LEVEL if chunk.audio.shape[1] else np.zeros(0)


class ModePart:
    """One mode's share of a received block (MultiModeReceiver)."""

    __slots__ = ("mode", "rate", "samples", "level_db", "noise_bw_hz")

    def __init__(self, mode, rate, samples, level_db, noise_bw_hz):
        self.mode, self.rate, self.samples = mode, rate, samples
        self.level_db, self.noise_bw_hz = level_db, noise_bw_hz


class ModeBundle:
    """One block of IQ as each listened mode's discriminator output, for
    the engine's `mode_in`. Its length is the block's at the link's rate,
    for the base class's receive queue."""

    def __init__(self, parts: list[ModePart], seconds: float, rate: float):
        self.parts, self.seconds = parts, seconds
        self._n = int(round(seconds * rate))

    def __len__(self) -> int:
        return self._n


class MultiModeReceiver:
    """An adaptive link's receiver (adapt.py): one FskChannelReceiver per
    mode it is told to listen to, each with its own channel filter and
    rate - a mode is only as robust as its own filter's bandwidth, so the
    slow modes cannot be taken from the fast ones' wide channel. Busy is
    any of them. The modes change between blocks (`listen`), from the
    engine's thread; the set is swapped whole, never edited in place."""

    def __init__(self, rate_iq: float, centre_hz: float, channel_hz: float, modes, *, sps: int = 8,
                 link_rate: float | None = None):
        from .. import modem
        self.rate_iq, self.centre_hz, self.channel_hz, self.sps = rate_iq, centre_hz, channel_hz, sps
        self.link_rate = link_rate or rate_iq
        self.modes = tuple(modes)
        self._modem = modem
        self._made: dict[str, FskChannelReceiver] = {}
        self._wanted: tuple[str, ...] = (self.modes[0], self.modes[-1])
        self.level_db: float | None = None
        self.busy: bool | None = None
        self.freq_hz: float | None = None

    def listen(self, modes) -> None:
        self._wanted = tuple(m for m in self.modes if m in set(modes))

    def _receiver(self, name: str) -> FskChannelReceiver:
        rx = self._made.get(name)
        if rx is None:
            m = self._modem.mode(name)
            rx = self._made[name] = FskChannelReceiver(self.rate_iq, self.centre_hz, self.channel_hz, baud=m.baud,
                                                       sps=self.sps, deviation_hz=m.deviation_hz,
                                                       occupied_hz=m.occupied_hz)
        return rx

    def hear(self, z: np.ndarray, index: int | None = None) -> ModeBundle:
        parts = []
        wanted = self._wanted
        for name in wanted:
            rx = self._receiver(name)
            out = rx.hear(z, index)
            parts.append(ModePart(name, rx.rate, out, rx.peak_level_db, rx.noise_bw_hz))
        # A receiver not listened to is dropped, so it starts afresh (its
        # filters and floor) if it is wanted again.
        for name in list(self._made):
            if name not in wanted:
                del self._made[name]
        floor = self._made.get(self.modes[-1])
        if floor is not None:
            self.level_db, self.freq_hz = floor.level_db, floor.freq_hz
        flags = [r.busy for r in self._made.values() if r.busy is not None]
        self.busy = any(flags) if flags else None
        return ModeBundle(parts, len(z) / self.rate_iq, self.link_rate)


class SdrRxLink(AudioRadioLink):
    """A receive-only station radio on an IqSource (module docstring).
    `source` is an unopened sdr.IqSource whose centre this link sets
    (`frequency - ifo_hz`); `kind` names it for the app ("rtlsdr",
    "rtltcp", "sdrplay", ...)."""

    rate = AUDIO_RATE
    RECEIVE_ONLY = True

    def __init__(self, source: IqSource, frequency: float, *, ifo_hz: float = 100_000.0, ppm: float = 0.0,
                 deviation_hz: float = 3_000.0, deemphasis_us: float | None = None, kind: str = "sdr", **kw):
        super().__init__(**kw)
        if not frequency:
            raise LinkRefused("setup", "an SDR link needs the channel's frequency")
        if not 25_000 <= abs(ifo_hz) <= source.rate / 2 - 25_000:
            raise LinkRefused("setup", f"the channel offset {ifo_hz} Hz must keep the receiver's centre 25 kHz or "
                                       "more outside the channel and the channel inside the capture")
        self.source = source
        self.kind = kind
        self.frequency = float(frequency)
        self.ifo_hz = float(ifo_hz)
        self.ppm = float(ppm)
        self.deviation_hz = float(deviation_hz)
        self.deemphasis_us = deemphasis_us
        self.receiver: FmChannelReceiver | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None

    @property
    def level_db(self) -> float | None:
        return self.receiver.level_db if self.receiver is not None else None

    def _refusal(self) -> str | None:
        return super()._refusal() or "receive-only"

    def _open_radio(self) -> None:
        self.source.centre = self.frequency - self.ifo_hz
        try:
            self.receiver = FmChannelReceiver(self.source.rate, self.source.centre, self.frequency,
                                              deviation_hz=self.deviation_hz, deemphasis_us=self.deemphasis_us,
                                              ppm=self.ppm)
            self.source.open()
        except SdrError as e:
            raise LinkRefused("radio-error", f"{e.reason}: {e.detail}") from None
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_iq, name=f"SdrRxLink {self.kind} IQ", daemon=True)
        self._reader.start()

    def _close_radio(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        try:
            self.source.close()
        finally:
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=2)

    def _read_iq(self) -> None:
        try:
            while not self._stop.is_set():
                block = self.source.read()
                if block is None or self._stop.is_set():
                    break
                pcm = self.receiver.hear(block.samples, block.index)
                if pcm:
                    self._rx(pcm)
        except Exception as e:                      # noqa: BLE001 - said once; the link stays open, silent
            if not self._stop.is_set():
                log.error("%s receiver failed: %s", self.kind, e)
                self.receiver.busy = None
                self._event("radio", "radio-error", {"what": "receive", "error": str(e)})

    def _radio_busy_now(self) -> bool | None:
        return self.receiver.busy if self.receiver is not None else None

    # Nothing here may transmit; the refusal above means these are never
    # reached, and they refuse again if they ever are.
    def _radio_key(self) -> None:
        raise LinkRefused("receive-only")

    def _radio_unkey(self) -> None:
        return None

    def _radio_audio(self, pcm: bytes) -> bool:
        return False
