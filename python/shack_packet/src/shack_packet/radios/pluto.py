"""A Pluto as a packet radio: FM transmit and receive on one channel,
through libiio, with the transmitter quiet unless this link is sending.

Where the Icom links hand audio to a radio that does its own FM, a Pluto
is a bare transceiver: this link is the FM modulator, the FM receiver and
the transmitter's guard all at once. The base class (base.py) still does
the keying rules - the station asked at keying and every tick, the
max_key_s limit, unkey on error and close, audio paced in 20 ms pieces -
and this class supplies the `_radio_*` operations underneath. Audio is
12 kHz s16 both ways, as IcomLink's.

**Transmit.** Each 20 ms piece of engine audio becomes 48 000 IQ samples
at 2.4 MS/s, phase-continuous across pieces and across overs (`FmModulator`
keeps its phase and filter history for the life of the link):

- 12 kHz -> 48 kHz through a windowed-sinc interpolator (97 taps; measured
  flat to 5 kHz, 58 dB down at 7 kHz, 80 dB and more from 8 kHz), then
  48 kHz -> 2.4 MS/s by linear interpolation. The on-air proof (scratch, 18/10/2026) went 48 kHz ->
  2.4 MS/s by sample-and-hold and a 50-tap boxcar, which is the same
  linear interpolation; doing the first step properly leaves the 12 kHz
  images of the tones (9.8 and 10.8 kHz) about 60 dB down instead of the
  27 dB a straight linear interpolation from 12 kHz would.
- Deviation: the engine's transmit level (channel.ChannelConfig.amplitude,
  0.5 of full scale) is `deviation_hz` (3 kHz), so a louder engine deviates
  proportionally more, clipped at MAX_DEVIATION_HZ. The proof normalised
  each over to its own peak; a per-piece normalisation would change level
  every 20 ms, so this scales by the nominal level instead.
- The channel is shifted up by `ifo_hz` (100 kHz) digitally and the TX LO
  set to frequency - ifo_hz, so the LO's leakage (a carrier at the LO
  frequency whatever the DAC sends) lands 100 kHz outside the channel
  rather than in the middle of it. Amplitude 0.7 of the DAC's 12 bits,
  as the proof, packed as int16 I,Q with the 12 bits at the top.

**No holes mid-over.** The IC-9700 played 20 ms gaps in its audio as gaps
on the air and nothing decoded (18/10/2026); a Pluto's DAC that runs out
of samples plays zeros, which is the same hole with the carrier off too.
So the IQ goes out from a writer thread of its own, fed by a queue the base
class's `_radio_audio` fills without ever blocking: the writer pushes one
fixed-size buffer (TX_BLOCK, 20 ms) at a time, and when the queue runs dry
with less than CARRIER_MARGIN_S still to play it pushes 20 ms of
unmodulated carrier rather than let the DAC starve. The carrier keeps the
receiver's squelch and PLL where they were; a frame cut by it is lost
either way, but the frames either side of it are not. Carrier that went
out between two pieces of audio is counted (`stats["tx_gaps"]`), apart from
the lead-in before the first piece and the tail after the last, which are
expected (`tx_fill_s` counts all of it). The kernel holds
TX_KERNEL_BUFFERS of them (320 ms; LEAD_S keeps about 250 ms queued), so
what is still to play when the base
asks (`_radio_drain_s`) is the queue plus what has been pushed beyond now,
and an unkey's drain is bounded by that.

**Receive** stops for each over and starts again on a fresh buffer the
moment the transmitter reads back off (`_read_iq`): the USB network then
carries one 9.6 MB/s stream, not two (`rx_during_tx` keeps it running, as
before 18/10/2026). RX LO also at frequency - ifo_hz,
so the receiver's DC offset and LO leakage are outside the channel too.
The receiver itself is radios.sdrrx.FmChannelReceiver, shared with the
receive-only SDRs (its docstring has the numbers). No de-emphasis by
default: the stations this was first built against are an IC-9700 sending
from its data port, which is flat, and the receiver's twist slicers take
+-9 dB an octave either way (demod.py); `deemphasis_us` turns on the
channeliser's.
`rx_gain` None is the AD936x's slow_attack AGC; a number is manual gain,
which the proof needed (25 dB) next to a strong local station that drove
the AGC into overload.

**The transmitter is quiet unless keyed, and is made so four ways:**

1. At open, the first writes are TX LO powerdown = 1 and TX hardwaregain
   = -89.75 dB, read back before anything else is set; a Pluto that does
   not read quiet is refused ("tx-not-quiet"), as sdr.PlutoSource does (a
   Pluto that has just booted can have its TX LO up at 2450 MHz with 10 dB
   of attenuation: GUIDE §5).
2. Every unkey writes both again and reads them back; a read-back that is
   not quiet raises, so the base marks the unkey owed and retries it every
   tick and tells the app ("unkey-failed"). The base unkeys on any error,
   disconnect and close, and the writer thread quiets the radio itself if
   a push fails, then raises the failure on the next tick (`_radio_alive`)
   so the base aborts the over as "radio-error".
3. An independent watchdog thread: if the TX LO has been up (counted from
   the moment powerdown = 0 was about to be written, by this module's own
   record, not the base's) for longer than max_key_s + WATCHDOG_GRACE_S,
   it quiets the transmitter itself. It never takes the link's lock - the
   base's supervisor may be the thing that is stuck, inside `_radio_unkey`
   holding it - and it writes through an iio Context of its own, opened to
   the same URI at open, because the main context may be the thing that is
   stuck: libiio over the network is one connection per context, and a
   push that hangs holds it. (If the second context cannot be had, it
   falls back to the main one and says so in the log.)
4. At close, after everything else, whatever else failed.

And the base class's two later safeguards have a Pluto answer each: the
unkey is confirmed by reading powerdown and gain back (`CONFIRMS_UNKEY`),
and its lock-free emergency unkey quiets through the watchdog's context
(`_emergency_unkey`), not the main one a stuck push holds. None of the
four ways, nor these, survives the process: a `kill -9` leaves the
transmitter as it was. The guard on the Pluto itself (shack_pluto.guard)
is what covers that. Given one (`guard`), the link starts it straight
after the first quiet, before any other transmit setting, and stops it
last at close; transmitting is refused ("tx-unguarded") whenever it is not
confirmed - at keying and every tick while keyed, so a guard that goes away
mid-over unkeys the radio as a disarm does. Given none, the link is
receive-only unless the caller says `allow_unguarded` (the tests, and the
two-station harness, which runs its own guard): fail closed by default.

**One gate for every attribute write** (`_write`), as PlutoSource's: the
receive settings it lists, and on the transmit side only the named
settings with the one value each may have here (TX LO frequency =
frequency - ifo_hz, TX rate = rate_iq, TX bandwidth = TX_BANDWIDTH, TX
gain = tx_gain_db or the quiet -89.75, powerdown 0 or 1). Anything else
raises before it reaches the radio. Close puts back the receive settings
it found and never a transmit setting other than quiet.

`tx_state()` reads powerdown and gain back from the radio for a harness
that wants to print them after every over.

The baseband rate is shared by RX and TX in the AD936x and must be a
multiple of 48 kHz here (the modulator's integer interpolation) and at
least sdr.pluto.NO_FIR_MIN (below that the chip needs its FIR, shared
with the transmitter). A stock Pluto runs as an AD9363 and is refused
outside 325 MHz-3.8 GHz with sdr.pluto's sentence; the board this was
proved on is a Rev.B clone set to AD9364 (70 MHz-6 GHz).

**The fast modes** (`baud` 9600, 19 200 or 38 400: fsk.py, G3RUH FSK;
or `mode`, the wide modes below).
12 kHz audio cannot carry 9600 baud, so with a `baud` the link carries the
modem's baseband itself, eight samples a bit (`rate` = 8 x baud: 76.8,
153.6 or 307.2 kHz), and the engine's modem is G3RUH (modem.py, from this
link's `baud`). Transmit is FskModulator: the shaped baseband is the
frequency, interpolated straight to IQ. Receive is sdrrx.FskChannelReceiver:
the channeliser's decimation lands exactly on the modem's rate and its
discriminator goes to fsk.Receiver as it is. Everything above - the
guard, the quiet after every over, receive paused during our own overs,
the watchdogs, the gate, the stats - is the same code; only the
modulator, the receiver and the rate differ.

The IQ rate is FSK_RATE_IQ, 2.4576 MS/s, not 2.4: it is 9600 x 256, so
every speed is a whole number of IQ samples a bit and a whole decimation
to eight samples a bit (32, 16 and 8). At 2.4 MS/s 38 400 baud is 62.5
samples a bit and no decimation gives a whole number of samples a bit,
which the transmitter needs. It is above NO_FIR_MIN, so the AD936x needs
no FIR (Tezuka refuses one), and only 2.4 % more than 2.4 MS/s over the
USB network, which carries one direction at a time here (GUIDE §5). A
lower rate was not worth it: below 2.083 MS/s is the FIR, and the CPU is
the channeliser's one big FFT, the same at either.

The LO stays `ifo_hz` (100 kHz) below the channel: 38 400 baud's 41.5 kHz
of occupied bandwidth then spans 79-121 kHz from the LO, well clear of
its leakage, inside TX_BANDWIDTH and the capture.

**The wide modes** (`mode`: gfsk76800, gfsk153600, 4fsk38400, 4fsk76800;
modem.py) are the same link at more samples a second of baseband - eight
a symbol, so 307.2 kHz to 1.2288 MHz, a decimation of 8, 4 or 2 from the
same 2.4576 MS/s - because receive is paused during our own overs and
only one IQ stream ever crosses the USB link: its limit is samples, not
baud. A mode wider than WIDE_OCCUPIED_HZ (gfsk153600 at 166 kHz,
4fsk76800 at 141) puts its channel WIDE_IFO_HZ (200 kHz) above the LO
rather than 100, which keeps the LO 117 kHz or more from the signal's 99 %
edge and outside the channel filter's skirt; and any mode asks the TX
analogue filter (`tx_bandwidth`, the one value the gate allows) for
1.25 x the signal's far edge either side, 750 kHz at gfsk153600. Both ends
use the same ifo_hz, so the far station's LO leakage lands on this one's
DC, which the AD936x's own DC tracking takes out. On the air the leakage
and the IQ image sit at frequency - ifo_hz and frequency - 2 x ifo_hz: a
frequency plan has to keep both inside the band segment, not just the
channel (README, the band plan).

The fast modes have been opened receive-only on both Plutos (shack's
Rev.B clone and workbench's Tezuka nano, 18/10/2026: TX read back quiet,
receive at 98.7-99.9 % of real time at 9600-38 400 baud, the on-device
guard started and stopped over paramiko and over OpenSSH) and never keyed:
tools/fastfsk_test.py is the keyed test, with Brian.

**PTT output** (`ptt_output`, off by default; asked for 18/10/2026 for an
external T/R relay or a PA's PTT): "gpo0".."gpo3" drives one of the
AD936x's GPO pins here, through ad9361-phy's debugfs direct_reg_access
(0x026 bit 4 manual mode, set at open and put back at close; 0x027 bits
4-7 the levels, only our bit ever changed, through the same kind of gate
as the attributes). An over is: GPO on (read back) -> `relay_lead_ms` ->
gain -> LO up -> samples -> quiet (RF off, read back) -> `relay_tail_ms`
-> GPO off. Every other quiet - the watchdogs, an error, close - drops the
GPO at once, and the guard on the Pluto, told `gpo`, drops all four with
its own quiet (a guard not told leaves the link receive-only). "firmware"
is for a Pluto whose firmware drives GPO0 itself: PlutoDVB2's
watchconsoletx.sh follows the TX LO (0.5.16.7, read on workshop's), and
Tezuka's (the same name, a different script, read on the nano 18/10/2026)
follows TX DMA interrupts, polling - both read-modify-write 0x027 bit 4.
There the over starts with the LO up at the quiet gain, the over's gain
goes up after the lead, and at the end the gain goes down, the tail
passes, and the LO goes down (and the samples stop), which drops the
firmware's GPO; the lead is added to the turnaround so the preamble covers
it. Tezuka's firmware owns all its GPOs - watchconsoletx.sh GPO0 for TX,
switch_lnb.sh GPO2/3 for LNB power (both read on the nano 18/10/2026) - so
gpo0-3 are refused on Tezuka; on PlutoDVB2 only "firmware" should be
offered (its other GPOs' uses are not known). The register read pointer is
shared with those scripts, so every register read is repeated until two
agree (REG_READ_TRIES). Which pins are brought out: not
verified on any board here (needs the schematic or a meter); measured
18/10/2026, receive-only: shack's Rev.B clone (ADI v0.39) has debugfs,
0x026 = 0x80 (not manual), 0x027 = 0, no F5OEO scripts; the nano (Tezuka)
0x026 = 0x10, 0x027 = 0, watchconsoletx.sh running.

**Amplifier** (`max_tx_gain_db`): a cap on the TX gain for a Pluto
driving an amplifier. A gain above it is refused at open and at every
write (the gate), and the guard on the Pluto, given the same cap, quiets
any gain it reads above it (a guard without it leaves the link
receive-only).

NOT YET TESTED ON HARDWARE as a link: the TX path is the on-air proof's,
the RX path the SDR monitor's; this class has run only against
tests/sdrhelp.FakePluto.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time

import numpy as np

from ..demod import _Fir
from ..errors import PacketError
from ..sdr.channeliser import _kaiser_lowpass
from ..sdr.pluto import AD9363_RANGE, AD9363_REFUSAL, FULL_SCALE, NO_FIR_MIN, RESTORE, RX_WRITES, TX_ATTEN_MAX, \
    _number, import_iio
from ..sdr.source import SdrError
from .base import AudioRadioLink, LinkRefused
from .sdrrx import FmChannelReceiver, FskChannelReceiver, MultiModeReceiver

log = logging.getLogger(__name__)

AUDIO_RATE = 12_000
TX_BANDWIDTH = 200_000           # the AD936x's TX analogue filter, as the proof set it
TX_BLOCK_S = 0.02                # one iio TX buffer: the base's 20 ms piece
TX_KERNEL_BUFFERS = 16           # 320 ms in the kernel at most: room for LEAD_S and a little
#: DMA and DAC pipeline beyond the writer's model, allowed in the drain: 50 ms,
#: near the 71 ms tail the first good keyed run had, so the last flags are
#: never cut.
TX_LATENCY_S = 0.05
CARRIER_MARGIN_S = 0.04          # push carrier once less than this is left to play
IQ_AMPLITUDE = 0.7               # of the DAC's full scale, as the proof
NOMINAL_LEVEL = 0.5              # the engine's transmit level (channel.ChannelConfig.amplitude)
MAX_DEVIATION_HZ = 5_000.0
RX_BLOCK_S = 0.05
RX_KERNEL_BUFFERS = 4
#: Receive recoveries (`_read_iq`): how many a minute before giving up, and
#: how many tries each, RX_REBUILD_WAIT_S apart (growing).
RX_REBUILDS = 5
#: Ten tries, 0.5 s growing to RX_REBUILD_MAX_WAIT_S apart: about 25 s, long
#: enough to meet a Pluto that has rebooted (the nano's took 22 s to come
#: back) and quiet it (`_fresh_context_ok`).
RX_REBUILD_TRIES = 10
RX_REBUILD_WAIT_S = 0.5
RX_REBUILD_MAX_WAIT_S = 3.0
WATCHDOG_GRACE_S = 1.0
#: xo_correction the link will write: 40 MHz +-200 ppm (shack_pluto.xo).
XO_RANGE = (39_992_000, 40_008_000)
WATCH_EVERY_S = 0.05
DDS = "cf-ad9361-dds-core-lpc"
#: The fast modes' IQ rate and samples a bit at the modem (module docstring).
#: PTT outputs (module docstring, "PTT output").
PTT_OUTPUTS = ("none", "gpo0", "gpo1", "gpo2", "gpo3", "firmware")
RELAY_MS_MAX = 500.0
#: AD936x registers (UG-672): 0x026 bit 4 puts the GPOs in manual mode,
#: 0x027 bits 4-7 are GPO0-3's levels in it. What PlutoDVB2's and
#: Tezuka's watchconsoletx.sh write.
REG_GPO_MODE = 0x26
REG_GPO = 0x27
GPO_MANUAL = 0x10
REG_READ_TRIES = 6
FSK_RATE_IQ = 2_457_600
FSK_SPS = 8
#: Modes wider than this (99 %) put the channel WIDE_IFO_HZ above the LO
#: rather than 100 kHz, so the LO stays 25 kHz or more outside the
#: channel and its filter's skirt (module docstring).
WIDE_OCCUPIED_HZ = 100_000.0
WIDE_IFO_HZ = 200_000.0


class FmModulator:
    """12 kHz audio (float, the engine's level NOMINAL_LEVEL) to FM IQ at
    `rate_iq`, centred `ifo_hz` above the LO. Keeps its phase and filter
    history between calls (module docstring)."""

    UP1 = 4                                   # 12 kHz -> 48 kHz by a real filter

    def __init__(self, rate_iq: int = 2_400_000, deviation_hz: float = 3_000.0, ifo_hz: float = 100_000.0,
                 amplitude: float = IQ_AMPLITUDE):
        mid = AUDIO_RATE * self.UP1
        if rate_iq % mid:
            raise ValueError(f"the IQ rate must be a multiple of {mid} S/s")
        self.rate_iq = int(rate_iq)
        self.up2 = self.rate_iq // mid
        self.hz_per_unit = deviation_hz / NOMINAL_LEVEL
        self.ifo_hz = float(ifo_hz)
        self.amplitude = amplitude
        # Flat to the tones' sidebands (4 kHz), falling by 8 kHz where
        # the 12 kHz images of 2.2 kHz start: 97 taps at 48 kHz.
        h = _kaiser_lowpass(24 * self.UP1 + 1, 6_000.0, mid, 6.5) * self.UP1
        self._fir = _Fir(h)
        self._last = 0.0                      # the last 48 kHz sample, for the linear step
        self._phase = 0.0
        self._t = np.arange(self.up2) / self.up2

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        x = np.zeros(len(audio) * self.UP1)
        x[::self.UP1] = audio
        y = self._fir(x)
        joined = np.concatenate([[self._last], y])
        self._last = float(y[-1]) if len(y) else self._last
        # Linear interpolation, sample k-1 to k over up2 steps.
        fine = (joined[:-1, None] * (1.0 - self._t) + joined[1:, None] * self._t).ravel()
        hz = np.clip(fine * self.hz_per_unit, -MAX_DEVIATION_HZ, MAX_DEVIATION_HZ) + self.ifo_hz
        phase = self._phase + np.cumsum(hz) * (2.0 * math.pi / self.rate_iq)
        if len(phase):
            self._phase = float(phase[-1] % (2.0 * math.pi))
        return self.amplitude * np.exp(1j * phase)


class FskModulator:
    """The fast modes' transmitter (fsk.py): the engine's shaped baseband at
    the modem's rate (`rate`, sps samples a bit; level NOMINAL_LEVEL is
    `deviation_hz`) straight to FM IQ at `rate_iq`, `ifo_hz` above the LO.
    No audio filter, unlike FmModulator: the baseband is already the
    Gaussian-shaped frequency of the signal (fsk.Modulator), band-limited to
    well under a tenth of its own rate, so linear interpolation up to the IQ
    rate adds nothing a receiver can find (its images sit at multiples of
    the modem rate, 77 kHz and more away, and are FM sidebands of a
    deviation under a hundredth of a radian). Phase carried between calls."""

    def __init__(self, rate_iq: int, rate: int, deviation_hz: float, ifo_hz: float = 100_000.0,
                 amplitude: float = IQ_AMPLITUDE):
        if rate_iq % rate:
            raise ValueError(f"the IQ rate must be a multiple of {rate} S/s")
        self.rate_iq = int(rate_iq)
        self.up = self.rate_iq // int(rate)
        self.hz_per_unit = deviation_hz / NOMINAL_LEVEL
        self.max_hz = 1.6 * deviation_hz
        self.ifo_hz = float(ifo_hz)
        self.amplitude = amplitude
        self._last = 0.0
        self._phase = 0.0
        self._t = np.arange(self.up) / self.up

    def __call__(self, baseband: np.ndarray) -> np.ndarray:
        joined = np.concatenate([[self._last], baseband])
        self._last = float(baseband[-1]) if len(baseband) else self._last
        fine = (joined[:-1, None] * (1.0 - self._t) + joined[1:, None] * self._t).ravel()
        hz = np.clip(fine * self.hz_per_unit, -self.max_hz, self.max_hz) + self.ifo_hz
        phase = self._phase + np.cumsum(hz) * (2.0 * math.pi / self.rate_iq)
        if len(phase):
            self._phase = float(phase[-1] % (2.0 * math.pi))
        return self.amplitude * np.exp(1j * phase)


def _iio_uri(uri: str) -> tuple[str, int | None]:
    """(libiio's URI, the SSH port a `pluto://` URI names or None)."""
    if not str(uri).startswith("pluto://"):
        return uri, None
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(uri)
    q = parse_qs(parts.query)
    try:
        iiod = int(q["iiod"][0]) if "iiod" in q else 30431
        ssh = int(q["ssh"][0]) if "ssh" in q else None
    except ValueError:
        raise LinkRefused("setup", f"{uri}: a port is not a number") from None
    if not parts.hostname:
        raise LinkRefused("setup", f"{uri} has no host")
    return (f"ip:{parts.hostname}" if iiod == 30431 else f"ip:{parts.hostname}:{iiod}"), ssh


def _reg_value(text) -> int | None:
    """direct_reg_access's answer ("0x10", or "16") as a number."""
    try:
        return int(str(text).strip().split()[-1], 0)
    except (ValueError, IndexError):
        return None


def iq_raw(iq: np.ndarray) -> bytes:
    """Complex IQ (full scale 1.0) as the AD936x DAC's int16 I,Q: 12 bits
    at the top of each word."""
    out = np.empty(2 * len(iq), np.int16)
    out[0::2] = np.round(np.clip(iq.real, -1.0, 1.0) * 2047.0).astype(np.int16) << 4
    out[1::2] = np.round(np.clip(iq.imag, -1.0, 1.0) * 2047.0).astype(np.int16) << 4
    return out.tobytes()


class PlutoLink(AudioRadioLink):
    """`frequency` is the channel on the air (Hz). See the module docstring
    for everything else; `iio_module` is a test's stand-in libiio."""

    rate = AUDIO_RATE
    #: How far ahead of real time the base class hands audio over (base.py
    #: LEAD_S, 60 ms for a rig). A Pluto is fed over its USB network by
    #: iiod on one ARM core, and a push can wait (29 ms seen on air,
    #: 18/10/2026): so up to 250 ms is queued in the kernel
    #: (TX_KERNEL_BUFFERS) before the DAC could run dry. At 150 ms the
    #: least queued in a keyed run was 15.6 ms, with one push 4.5 ms late. The unkey still waits for exactly what was queued
    #: (`_radio_drain_s`), so a longer lead is not a longer tail.
    LEAD_S = 0.25

    def __init__(self, uri: str = "ip:192.168.2.1", frequency: float | None = None, *, ifo_hz: float | None = None,
                 rate_iq: int | None = None, deviation_hz: float | None = None, tx_gain_db: float = -10.0,
                 rx_gain: float | None = None, rx_port: str = "A_BALANCED", deemphasis_us: float | None = None,
                 guard=None, allow_unguarded: bool = False, xo_correction: int | None = None,
                 transmit: bool = True, rx_during_tx: bool = False, baud: int = 1200, mode: str | None = None,
                 modes=None, ptt_output: str = "none", relay_lead_ms: float = 20.0, relay_tail_ms: float = 10.0,
                 max_tx_gain_db: float | None = None, iio_module=None, **kw):
        super().__init__(**kw)
        if not frequency:
            raise LinkRefused("setup", "a Pluto link needs the channel's frequency")
        from .. import modem
        try:
            if modes:
                # Adaptive (adapt.py): the ladder, fastest first; the link
                # is set up for its top and carries every mode on it.
                modes = tuple(modem.mode(x).name for x in modes)
                mode = modes[0]
            m = modem.mode(mode) if mode is not None else modem.mode(modem.check(baud))
        except PacketError as e:
            raise LinkRefused("setup", str(e)) from None
        #: The ladder on an adaptive link (adapt.py), else None.
        self.modes = modes or None
        ladder = [modem.mode(x) for x in (modes or (m.name,))]
        if modes and not all(x.fast for x in ladder):
            raise LinkRefused("setup", "an adaptive link runs the fast modes only")
        #: The modem (modem.Mode); `mode` its name and `baud` its bit rate,
        #: as the engine reads them (modem.of_link).
        self.modem = m
        self.mode = m.name
        self.baud = m.bitrate
        fast = m.fast
        if rate_iq is None:
            rate_iq = FSK_RATE_IQ if fast else 2_400_000
        if deviation_hz is None:
            # An adaptive link's modulator is set for the widest deviation
            # on its ladder; each part of an over is scaled to its own
            # (channel.py).
            deviation_hz = max(x.deviation_hz for x in ladder)
        widest = max(ladder, key=lambda x: x.occupied_hz)
        if ifo_hz is None:
            ifo_hz = WIDE_IFO_HZ if widest.occupied_hz > WIDE_OCCUPIED_HZ else 100_000.0
        #: The engine's audio (AFSK) or baseband (FSK) rate, both ways:
        #: FSK_SPS samples a symbol.
        self.rate = m.baud * FSK_SPS if fast else AUDIO_RATE
        if modes:
            slow = [x.name for x in ladder if self.rate % x.baud or self.rate // x.baud < FSK_SPS]
            if slow:
                raise LinkRefused("setup", f"{', '.join(slow)} cannot be sent at {self.rate} samples a second: "
                                           f"the ladder's top must have the fastest symbol rate")
        step = self.rate if fast else AUDIO_RATE * FmModulator.UP1
        tx_gain_db = float(tx_gain_db)
        if not TX_ATTEN_MAX <= tx_gain_db <= 0.0:
            raise LinkRefused("setup", f"TX gain {tx_gain_db} dB is outside {TX_ATTEN_MAX}-0 dB")
        #: An amplifier's drive cap (module docstring, "Amplifier"): no TX
        #: gain above it is ever written, and the guard is asked to keep it.
        self.max_tx_gain_db = None if max_tx_gain_db is None else float(max_tx_gain_db)
        if self.max_tx_gain_db is not None and tx_gain_db > self.max_tx_gain_db:
            raise LinkRefused("setup", f"TX gain {tx_gain_db} dB is above the amplifier's drive cap "
                                       f"{self.max_tx_gain_db} dB")
        rate_iq = int(rate_iq)
        if rate_iq < NO_FIR_MIN or rate_iq % step:
            raise LinkRefused("setup", f"the IQ rate must be at least {NO_FIR_MIN} S/s and a multiple of "
                                       f"{step} S/s, not {rate_iq}")
        half = widest.occupied_hz / 2.0
        if not 25_000 + half <= abs(ifo_hz) <= rate_iq / 2 - 25_000 - half:
            raise LinkRefused("setup", f"the channel offset {ifo_hz} Hz must keep the LO 25 kHz or more outside "
                                       f"the channel and the channel inside the capture")
        #: The URI libiio is given: a `pluto://host?iiod=..&ssh=..` one (a
        #: Pluto shared by the controller box, shack_pluto.endpoint) becomes
        #: `ip:host[:port]`; its SSH port is kept for whoever makes the guard.
        self.uri, self.ssh_port = _iio_uri(uri)
        self.frequency = float(frequency)
        self.ifo_hz = float(ifo_hz)
        self.lo_hz = int(round(self.frequency - self.ifo_hz))
        self.rate_iq = rate_iq
        self.deviation_hz = float(deviation_hz)
        self.tx_gain_db = tx_gain_db
        self.rx_gain = rx_gain
        self.rx_port = rx_port
        self.deemphasis_us = deemphasis_us
        self._iio = iio_module
        self._lib = None
        #: The transmitter guard on the Pluto (shack_pluto.guard.Guard, or
        #: anything with start(), stop() and `confirmed`), and why transmit
        #: is off when it is: None while a guard is confirmed.
        self.guard = guard
        #: The PTT output for an external T/R relay or PA (module docstring,
        #: "PTT output"): "none", "gpo0".."gpo3" driven here, or "firmware"
        #: for the GPO0 the firmware's own script drives (PlutoDVB2, Tezuka).
        if ptt_output not in PTT_OUTPUTS:
            raise LinkRefused("setup", f"PTT output {ptt_output!r} is not one of {', '.join(PTT_OUTPUTS)}")
        lead, tail = float(relay_lead_ms), float(relay_tail_ms)
        if not (0.0 <= lead <= RELAY_MS_MAX and 0.0 <= tail <= RELAY_MS_MAX):
            raise LinkRefused("setup", f"relay lead and tail are 0-{RELAY_MS_MAX:g} ms")
        self.ptt_output = ptt_output
        self.relay_lead_s, self.relay_tail_s = lead / 1000.0, tail / 1000.0
        self._gpo_bit = 1 << (4 + int(ptt_output[3])) if ptt_output.startswith("gpo") else 0
        self._gpo_found: dict = {}
        self._gain_pending = False
        self._quiet_seq = 0
        self._dbg = None
        self._watch_dbg = None
        if ptt_output == "firmware":
            # The firmware raises its GPO when the over starts (the LO, or
            # samples flowing) and the over begins at the quiet gain for
            # the lead: the preamble has to cover it.
            self.turnaround_s += self.relay_lead_s
        #: This Pluto's crystal correction (shack_pluto.xo), written at open
        #: and put back as found at close; None leaves the radio's own. Or a
        #: callable given the context's attributes at open (hw_serial...),
        #: returning the value or None: the app's per-Pluto store, asked
        #: about the Pluto actually plugged in rather than the one the
        #: settings were made with.
        if xo_correction is not None and not callable(xo_correction):
            xo_correction = int(xo_correction)
            if not XO_RANGE[0] <= xo_correction <= XO_RANGE[1]:
                raise LinkRefused("setup", f"crystal correction {xo_correction} is more than 200 ppm from 40 MHz")
        self.xo_correction = xo_correction
        #: The value written at this open, or None.
        self.xo_applied: int | None = None
        self._xo_found = None
        self.allow_unguarded = bool(allow_unguarded)
        #: False: the app has not set transmit up, so this link is
        #: receive-only by choice - no guard is started and keying is refused
        #: as "receive-only", with nothing to warn about.
        self.transmit = bool(transmit)
        #: Whether to keep receiving through our own overs (`_read_iq`).
        self.rx_during_tx = bool(rx_during_tx)
        self.unguarded_why: str | None = None
        #: The TX analogue filter asked for: TX_BANDWIDTH, or wide enough
        #: for a wide mode's far edge (ifo + half its occupied bandwidth)
        #: with a quarter to spare, in 50 kHz steps.
        edge = 2.0 * (abs(self.ifo_hz) + half) * 1.25
        self.tx_bandwidth = max(TX_BANDWIDTH, int(math.ceil(edge / 50_000.0)) * 50_000)
        self.tx_block = int(round(rate_iq * TX_BLOCK_S))
        self.rx_block = int(round(rate_iq * RX_BLOCK_S))
        #: The gate's transmit half: each setting this link may write, and
        #: the values it may have (module docstring).
        self.tx_allowed = {
            ("tx_lo", "powerdown"): {"0", "1"},
            ("tx", "hardwaregain"): {str(TX_ATTEN_MAX), str(tx_gain_db)},
            ("tx_lo", "frequency"): {str(self.lo_hz)},
            ("tx", "sampling_frequency"): {str(rate_iq)},
            ("tx", "rf_bandwidth"): {str(self.tx_bandwidth)},
        }
        self.identity = ""
        self.ctx = None
        self.chans: dict = {}
        self.found: dict = {}
        self.setup: dict = {}
        self._dds = None
        self._watch_ctx = None
        self._watch_chans: dict = {}
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        #: When the TX LO was (about to be) powered up, by time.monotonic:
        #: the independent watchdog's own record, cleared only by a quiet
        #: that read back.
        self._tx_on_since: float | None = None
        self._mod = (FskModulator(rate_iq, self.rate, deviation_hz, ifo_hz) if fast
                     else FmModulator(rate_iq, deviation_hz, ifo_hz))
        self._txq: queue.Queue = queue.Queue()
        self._tx_queued = 0                    # audio samples in the queue
        self._tx_until = 0.0                   # time.monotonic when what was pushed has played
        #: time.monotonic when the last sample of engine audio pushed will
        #: have played, by the same model as _tx_until; None before any.
        self._audio_end_at: float | None = None
        #: Set as an unkey's quiet reads back; cleared by the first receive
        #: block after it (`timing`).
        self._rx_wait_since: float | None = None
        self._timing: dict = {}
        self._tx_stop: threading.Event | None = None
        self._tx_thread: threading.Thread | None = None
        self._tx_error: BaseException | None = None
        self._rxbuf = None
        self._rx_stop = threading.Event()
        #: Set while the receiver may read; cleared for an over unless
        #: rx_during_tx (see `_read_iq`).
        self._rx_go = threading.Event()
        self._rx_go.set()
        #: Set once receive blocks are arriving, cleared while a failed
        #: receiver is being made again: a harness's "listening".
        self.rx_listening = threading.Event()
        #: The context a remade receive buffer lives on, when not the main one.
        self._rx_ctx = None
        self._rx_thread: threading.Thread | None = None
        #: The receiver (radios.sdrrx, shared with the receive-only SDRs).
        self.receiver: FmChannelReceiver | None = None
        self.last_tx_state: dict = {}
        self._listen: set | None = None

    # -- the gate ---------------------------------------------------------------------

    def _write(self, chan: str, name: str, value, chans: dict | None = None) -> None:
        text = str(value)
        if chan in ("tx", "tx_lo"):
            if text not in self.tx_allowed.get((chan, name), ()):
                raise PacketError(f"refused: {chan} {name}={text} is not a transmitter setting this link makes")
            if (name == "hardwaregain" and self.max_tx_gain_db is not None
                    and float(text) > self.max_tx_gain_db + 1e-9):
                raise PacketError(f"refused: TX gain {text} dB is above the amplifier's cap {self.max_tx_gain_db} dB")
        elif (chan, name) not in RX_WRITES:
            raise PacketError(f"refused: {chan} {name} is not a receive setting")
        try:
            (chans if chans is not None else self.chans)[chan].attrs[name].value = text
        except (KeyError, OSError, AttributeError) as e:
            raise SdrError("setup-failed", f"Pluto {self.uri}: {chan} {name}={text} ({e})") from e

    def _read_attr(self, chan: str, name: str, chans: dict | None = None):
        try:
            return (chans if chans is not None else self.chans)[chan].attrs[name].value
        except (KeyError, OSError, AttributeError):
            return None

    def _quiet_rf(self) -> bool:
        """Quiet the RF and leave the PTT output to the caller (the unkey's
        relay tail)."""
        return self._quiet(gpo=False)

    def _quiet(self, chans: dict | None = None, gpo: bool = True) -> bool:
        """Quiet the transmitter through `chans` (the main context's by
        default), both writes tried whatever the first did, and say whether
        it reads back quiet; and, unless `gpo` is False, drop the PTT output."""
        # A firmware-PTT gain still to come never comes (`_lead_gain`). No
        # lock: a quiet must never wait behind anything.
        self._gain_pending = False
        self._quiet_seq += 1
        ok = True
        for chan, name, value in (("tx_lo", "powerdown", "1"), ("tx", "hardwaregain", TX_ATTEN_MAX)):
            try:
                self._write(chan, name, value, chans)
            except (SdrError, PacketError) as e:
                log.error("quieting the Pluto's transmitter: %s", e)
                ok = False
        state = self._tx_read(chans)
        quiet = ok and state["quiet"]
        if gpo and self._gpo_bit and not self._gpo_set(False, watch=chans is not None and chans is self._watch_chans):
            log.error("quieting the Pluto: the PTT output did not read back off")
            quiet = False
        if quiet:
            self._tx_on_since = None
        return quiet

    # -- the PTT output (module docstring) -------------------------------------------------

    @staticmethod
    def _debug_reg(ctx):
        try:
            phy = ctx.find_device("ad9361-phy")
            return phy.debug_attrs["direct_reg_access"]
        except (AttributeError, KeyError, OSError):
            return None

    def _reg_read(self, reg: int, watch: bool = False) -> int | None:
        """A register through direct_reg_access: its address written (the
        driver's read pointer, not a register), then read - until two reads
        in a row agree. The pointer is one for every process on the Pluto,
        and Tezuka's and PlutoDVB2's scripts move it to 0x27 every second:
        on the nano a single read of 0x026 came back as 0x027's value
        (18/10/2026)."""
        dbg = (self._watch_dbg if watch else None) or self._dbg
        if dbg is None:
            return None
        last = None
        for _ in range(REG_READ_TRIES):
            try:
                dbg.value = hex(reg)
                value = _reg_value(dbg.value)
            except (OSError, AttributeError):
                return None
            if value is not None and value == last:
                return value
            last = value
        return None

    def _reg_write(self, reg: int, value: int, watch: bool = False) -> None:
        """The gate's register half: only the GPO mode register, set to
        manual or put back as found, and the GPO register with nothing but
        our own bit changed."""
        allowed = False
        if reg == REG_GPO_MODE:
            found = self._gpo_found.get(REG_GPO_MODE)
            allowed = value in ((found or 0) | GPO_MANUAL, found)
        elif reg == REG_GPO and self._gpo_bit:
            current = self._reg_read(REG_GPO, watch)
            allowed = current is not None and (value ^ current) & ~self._gpo_bit == 0
        if not allowed:
            raise PacketError(f"refused: register {hex(reg)}={hex(value)} is not a PTT output setting this link makes")
        dbg = (self._watch_dbg if watch else None) or self._dbg
        try:
            dbg.value = f"{hex(reg)} {hex(value)}"
        except (OSError, AttributeError) as e:
            raise SdrError("setup-failed", f"Pluto {self.uri}: register {hex(reg)} ({e})") from e

    def _gpo_set(self, on: bool, watch: bool = False) -> bool:
        """Our GPO up or down, read back. True when it reads as asked."""
        current = self._reg_read(REG_GPO, watch)
        if current is None:
            return False
        want = (current | self._gpo_bit) if on else (current & ~self._gpo_bit)
        if want != current:
            try:
                self._reg_write(REG_GPO, want, watch)
            except (SdrError, PacketError) as e:
                log.error("the PTT output: %s", e)
                return False
        after = self._reg_read(REG_GPO, watch)
        return after is not None and bool(after & self._gpo_bit) == on

    def _lead_gain(self) -> None:
        """Firmware PTT: the over's gain once the relay's lead has passed,
        unless a quiet came first; and if one came while the gain was being
        written, the quiet again, so a quiet is never undone."""
        time.sleep(self.relay_lead_s)
        seq = self._quiet_seq
        if not self._gain_pending:
            return
        self._gain_pending = False
        try:
            self._write("tx", "hardwaregain", self.tx_gain_db)
        except (SdrError, PacketError) as e:
            log.error("the over's gain after the relay lead: %s", e)
        if self._quiet_seq != seq:
            # A quiet ran while the gain went up: quiet again, after it.
            self._quiet()

    # -- turned live (0.3.0's control strip) ----------------------------------------------

    def set_tx_gain(self, db: float) -> None:
        """The next over's TX gain. Checked as at opening (range, and the
        amplifier's drive cap) and refused with PacketError otherwise. The
        gate's allowed value moves with it, so nothing but this value and
        the quiet TX_ATTEN_MAX can ever be written; the gain is written as
        each over keys (and after the relay's lead), never between overs,
        so a change mid-over takes effect at the next one."""
        db = float(db)
        if not TX_ATTEN_MAX <= db <= 0.0:
            raise PacketError(f"refused: TX gain {db} dB is outside {TX_ATTEN_MAX}-0 dB")
        if self.max_tx_gain_db is not None and db > self.max_tx_gain_db + 1e-9:
            raise PacketError(f"refused: TX gain {db} dB is above the amplifier's cap {self.max_tx_gain_db} dB")
        allowed = dict(self.tx_allowed)
        allowed[("tx", "hardwaregain")] = {str(TX_ATTEN_MAX), str(db)}
        self.tx_allowed = allowed
        self.tx_gain_db = db
        log.info("Pluto %s: TX gain %.1f dB from the next over", self.uri, db)

    def set_rx_gain(self, db: float | None) -> None:
        """Receive gain now: None the AD936x's slow AGC, a number manual dB."""
        self.rx_gain = None if db is None else float(db)
        if not self.chans:
            return
        if self.rx_gain is None:
            self._write("rx", "gain_control_mode", "slow_attack")
        else:
            self._write("rx", "gain_control_mode", "manual")
            self._write("rx", "hardwaregain", self.rx_gain)
        log.info("Pluto %s: RX gain %s", self.uri, "AGC" if self.rx_gain is None else f"{self.rx_gain:.0f} dB")

    def gpo_state(self) -> dict | None:
        """The GPO registers as read now (a harness's receive-only check):
        manual mode, each GPO's level. None without direct_reg_access."""
        mode, levels = self._reg_read(REG_GPO_MODE), self._reg_read(REG_GPO)
        if mode is None or levels is None:
            return None
        return {"manual": bool(mode & GPO_MANUAL), **{f"gpo{i}": bool(levels & (1 << (4 + i))) for i in range(4)},
                "ptt_output": self.ptt_output}

    def _tx_read(self, chans: dict | None = None) -> dict:
        pd = _number(self._read_attr("tx_lo", "powerdown", chans))
        gain = _number(self._read_attr("tx", "hardwaregain", chans))
        state = {"powerdown": pd, "gain_db": gain,
                 "quiet": pd == 1 and gain is not None and gain <= TX_ATTEN_MAX + 0.01}
        self.last_tx_state = state
        return state

    def tx_state(self) -> dict:
        """The transmitter as read back from the radio now: powerdown (1 is
        off), gain_db, and quiet (both at their quiet values). Through the
        watchdog's context when there is one, so a harness asking does not
        queue behind a push.

        Once closed there is nothing to read through, and a read then would
        say "not quiet" about a radio that read back quiet a moment ago (the
        two-station harness's first real-Pluto run printed exactly that): so
        a closed link answers with the read-back made as it closed, marked
        `closed`."""
        if not self.chans:
            return {**(self.last_tx_state or {"powerdown": None, "gain_db": None, "quiet": False}), "closed": True}
        return dict(self._tx_read(self._watch_chans or None))

    @property
    def tx_quiet(self) -> bool:
        return self.tx_state()["quiet"]

    # -- open and close -------------------------------------------------------------

    def _open_radio(self) -> None:
        try:
            iio = self._iio or import_iio()
        except SdrError as e:
            raise LinkRefused("setup", e.detail) from None
        self._lib = iio
        try:
            self.ctx = iio.Context(self.uri)
        except OSError as e:
            raise LinkRefused("radio-error", f"no Pluto at {self.uri} ({e})") from None
        try:
            self._setup()
        except BaseException:
            self._release()
            raise

    def _channels(self, ctx) -> dict:
        phy = ctx.find_device("ad9361-phy")
        if phy is None:
            return {}
        return {"rx_lo": phy.find_channel("altvoltage0", True), "tx_lo": phy.find_channel("altvoltage1", True),
                "rx": phy.find_channel("voltage0", False), "tx": phy.find_channel("voltage0", True)}

    def _setup(self) -> None:
        try:
            self.ctx.set_timeout(3000)
        except (AttributeError, OSError):
            pass
        attrs = getattr(self.ctx, "attrs", {}) or {}
        self.identity = str(attrs.get("ad9361-phy,model", "") or "").lower()
        self.chans = self._channels(self.ctx)
        rxdev = self._rxdev = self.ctx.find_device("cf-ad9361-lpc")
        self._dds = self.ctx.find_device(DDS)
        if not self.chans or any(c is None for c in self.chans.values()) or rxdev is None or self._dds is None:
            raise LinkRefused("setup", f"{self.uri} is not an AD936x transceiver "
                                       f"(no ad9361-phy, cf-ad9361-lpc or {DDS})")
        self._dbg = self._debug_reg(self.ctx)
        if self._gpo_bit and self._dbg is None:
            raise LinkRefused("setup", f"Pluto {self.uri}: the PTT output needs the AD936x's direct_reg_access "
                                       "(debugfs), which this Pluto does not offer")
        # The transmitter first, before anything else is written.
        if not self._quiet():
            raise LinkRefused("tx-not-quiet", f"Pluto {self.uri}: TX LO powerdown / attenuation did not read back")
        fw = str(attrs.get("fw_version", "") or "").lower()
        if self._gpo_bit and fw.startswith("tezuka"):
            # Its scripts own the GPOs: watchconsoletx.sh GPO0 (TX), and
            # switch_lnb.sh GPO2/3 (LNB power; read on the nano 18/10/2026).
            raise LinkRefused("setup", f"Pluto {self.uri}: Tezuka firmware drives the GPOs itself; choose the "
                                       "PTT output \"firmware\" (its GPO0)")
        if self._gpo_bit:
            self._gpo_found = {REG_GPO_MODE: self._reg_read(REG_GPO_MODE)}
            mode = self._gpo_found[REG_GPO_MODE]
            if mode is None:
                raise LinkRefused("setup", f"Pluto {self.uri}: the GPO mode register did not read")
            if not mode & GPO_MANUAL:
                self._reg_write(REG_GPO_MODE, mode | GPO_MANUAL)
        self._start_guard()
        lo, hi = AD9363_RANGE
        if self.identity == "ad9363" and not (lo <= self.frequency <= hi and lo <= self.lo_hz <= hi):
            raise LinkRefused("out-of-range", AD9363_REFUSAL)
        self.found = {key: self._read_attr(*key) for key in RESTORE}
        try:
            self._set_xo()
            self._setup_rx()
            self._setup_tx()
        except SdrError as e:
            raise LinkRefused("setup", e.detail) from None
        self._open_watch_context()
        for i in (0, 1):
            rxdev.find_channel(f"voltage{i}", False).enabled = True
        try:
            rxdev.set_kernel_buffers_count(RX_KERNEL_BUFFERS)
        except (AttributeError, OSError):
            pass
        try:
            self._rxbuf = self._lib.Buffer(rxdev, self.rx_block, False)
        except OSError as e:
            raise LinkRefused("radio-error", f"Pluto {self.uri}: the receive buffer ({e}); "
                                             "is another program using it?") from None
        self._rx_stop.clear()
        self._rx_thread = threading.Thread(target=self._read_iq, name="PlutoLink IQ", daemon=True)
        self._rx_thread.start()
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch, name="PlutoLink TX watchdog", daemon=True)
        self._watch_thread.start()

    def _start_guard(self) -> None:
        """The guard on the Pluto, before any other transmit setting is
        written. A guard that will not start leaves the link receive-only,
        said once as a link event, rather than refusing the radio: hearing
        the channel is still worth having (PLAN-0.2 §3.6.1)."""
        self.unguarded_why = None
        if not self.transmit:
            return
        cap = getattr(self.guard, "max_gain_db", None)
        if self.max_tx_gain_db is not None and self.guard is not None and (cap is None or cap > self.max_tx_gain_db):
            self.unguarded_why = "guard-no-cap: the transmitter guard was not given the amplifier's drive cap"
            log.warning("Pluto %s opens receive-only: %s", self.uri, self.unguarded_why)
            self._event("radio", "tx-unguarded", {"why": self.unguarded_why})
            return
        if self._gpo_bit and self.guard is not None and not getattr(self.guard, "gpo", False):
            # A guard that does not drop the GPO leaves a relay closed (or
            # a PA keyed) if this process dies: as good as none.
            self.unguarded_why = "guard-no-gpo: the transmitter guard was not told about the PTT output"
            log.warning("Pluto %s opens receive-only: %s", self.uri, self.unguarded_why)
            self._event("radio", "tx-unguarded", {"why": self.unguarded_why})
            return
        if self.guard is None:
            if not self.allow_unguarded:
                # The caller's choice, not a failure: refused at keying and
                # said in the log, but no event for the operator.
                self.unguarded_why = "no transmitter guard was given for this Pluto"
                log.info("Pluto %s opens receive-only: no transmitter guard was given", self.uri)
            return
        else:
            try:
                self.guard.start()
            except Exception as e:                  # noqa: BLE001 - any failure: receive-only, said
                reason = getattr(e, "reason", "guard-failed")
                self.unguarded_why = f"{reason}: {getattr(e, 'detail', '') or e}"
        if self.unguarded_why is not None:
            log.warning("Pluto %s opens receive-only: %s", self.uri, self.unguarded_why)
            self._event("radio", "tx-unguarded", {"why": self.unguarded_why})

    @property
    def guard_state(self) -> str:
        """For a status line: "off" (transmit not set up), "running",
        "none" (no guard given) or "failed" (unguarded_why says why)."""
        if not self.transmit:
            return "off"
        if self.guard is None:
            return "none"
        return "running" if self.tx_guarded else "failed"

    @property
    def tx_guarded(self) -> bool:
        """Whether transmitting is allowed by the guard right now."""
        if self.guard is None:
            return self.allow_unguarded
        return self.unguarded_why is None and bool(self.guard.confirmed)

    def _refusal(self) -> str | None:
        # Asked at keying and every tick while keyed (base.py), so a guard
        # that goes away mid-over unkeys within a tick, as a disarm does.
        why = super()._refusal()
        if why is None and not self.transmit:
            return "receive-only"
        if why is None and not self.tx_guarded:
            return "tx-unguarded"
        return why

    def _phy_attr(self, ctx=None):
        phy = (ctx or self.ctx).find_device("ad9361-phy")
        return None if phy is None else (getattr(phy, "attrs", {}) or {}).get("xo_correction")

    def _set_xo(self) -> None:
        """The crystal correction, before either LO is tuned: it moves both.
        A run-time attribute (the Pluto forgets it at boot), so it is put
        back as found at close, as the receive settings are."""
        self.xo_applied = None
        value = self.xo_correction
        if callable(value):
            try:
                value = value(dict(getattr(self.ctx, "attrs", {}) or {}))
            except Exception as e:                  # noqa: BLE001 - a broken store is no correction, said
                log.warning("Pluto %s: the stored crystal correction could not be read: %s", self.uri, e)
                value = None
            if value is not None and not XO_RANGE[0] <= int(value) <= XO_RANGE[1]:
                log.warning("Pluto %s: stored crystal correction %s is out of range; not used", self.uri, value)
                value = None
        if value is None:
            return
        attr = self._phy_attr()
        if attr is None:
            raise SdrError("setup-failed", f"Pluto {self.uri}: no xo_correction attribute")
        try:
            self._xo_found = str(attr.value).strip()
            attr.value = str(int(value))
            self.xo_applied = int(value)
        except OSError as e:
            raise SdrError("setup-failed", f"Pluto {self.uri}: xo_correction={value} ({e})") from e

    def _restore_xo(self) -> None:
        found, self._xo_found = self._xo_found, None
        if found is None or self.ctx is None:
            return
        try:
            v = int(float(found))
            if XO_RANGE[0] <= v <= XO_RANGE[1]:
                self._phy_attr().value = str(v)
        except (OSError, ValueError, AttributeError) as e:
            log.warning("Pluto %s: putting the crystal correction back: %s", self.uri, e)

    def _setup_rx(self) -> None:
        self._write("rx", "sampling_frequency", self.rate_iq)
        self._write("rx", "rf_bandwidth", self.rate_iq)
        try:
            self._write("rx", "rf_port_select", self.rx_port)
        except SdrError as e:
            self.setup["rx_port"] = e.detail
        for name in ("quadrature_tracking_en", "rf_dc_offset_tracking_en", "bb_dc_offset_tracking_en"):
            try:
                self._write("rx", name, 1)
            except SdrError:
                pass
        if self.rx_gain is None:
            self._write("rx", "gain_control_mode", "slow_attack")
        else:
            self._write("rx", "gain_control_mode", "manual")
            self._write("rx", "hardwaregain", float(self.rx_gain))
        self._write("rx_lo", "frequency", self.lo_hz)
        if self.modes:
            self.receiver = MultiModeReceiver(self.rate_iq, self.lo_hz, self.frequency, self.modes, sps=FSK_SPS,
                                              link_rate=self.rate)
            if self._listen is not None:
                self.receiver.listen(self._listen)
        elif self.modem.fast:
            self.receiver = FskChannelReceiver(self.rate_iq, self.lo_hz, self.frequency, baud=self.modem.baud,
                                               sps=FSK_SPS, deviation_hz=self.deviation_hz,
                                               occupied_hz=self.modem.occupied_hz)
        else:
            self.receiver = FmChannelReceiver(self.rate_iq, self.lo_hz, self.frequency,
                                              deviation_hz=self.deviation_hz, deemphasis_us=self.deemphasis_us)

    def _setup_tx(self) -> None:
        """The transmit side's settings, made with it quiet: rate (shared
        with RX in the chip, so normally already right), bandwidth, LO."""
        if _number(self._read_attr("tx", "sampling_frequency")) != self.rate_iq:
            self._write("tx", "sampling_frequency", self.rate_iq)
        self._write("tx", "rf_bandwidth", self.tx_bandwidth)
        self._write("tx_lo", "frequency", self.lo_hz)
        for i in (0, 1):
            self._dds.find_channel(f"voltage{i}", True).enabled = True

    def _open_watch_context(self) -> None:
        try:
            ctx = self._lib.Context(self.uri)
            chans = self._channels(ctx)
            if not chans.get("tx") or not chans.get("tx_lo"):
                raise OSError("no AD936x on the second context")
            self._watch_ctx = ctx
            self._watch_chans = {"tx": chans["tx"], "tx_lo": chans["tx_lo"]}
            self._watch_dbg = self._debug_reg(ctx)
        except Exception as e:                      # noqa: BLE001 - the main context will do, said
            log.warning("Pluto %s: no second context for the TX watchdog (%s); it will use the main one",
                        self.uri, e)
            self._watch_ctx = None
            self._watch_chans = {}

    def _close_radio(self) -> None:
        try:
            self._stop_writer()
            self._stop_receiver()
            self._restore_rx()
            self._restore_xo()
            self._restore_gpo_mode()
        finally:
            self._release()

    def _restore_gpo_mode(self) -> None:
        """The GPO mode register as found, once our GPO is down."""
        found = self._gpo_found.get(REG_GPO_MODE)
        if found is None or self.ctx is None or not self._gpo_set(False):
            return
        try:
            if self._reg_read(REG_GPO_MODE) != found:
                self._reg_write(REG_GPO_MODE, found)
        except (SdrError, PacketError) as e:
            log.warning("Pluto %s: putting the GPO mode back: %s", self.uri, e)

    def _restore_rx(self) -> None:
        if not (self.ctx is not None and self.chans and self.found):
            return
        for (chan, name) in RESTORE:
            value = self.found.get((chan, name))
            v = _number(value)
            if value is None or (name != "gain_control_mode" and v is None):
                continue
            if name == "hardwaregain" and str(self.found.get(("rx", "gain_control_mode"))) != "manual":
                continue           # the driver only takes a gain in manual mode
            try:
                self._write(chan, name, value if name == "gain_control_mode" else
                            (v if name == "hardwaregain" else int(v)))
            except SdrError:
                pass

    def _release(self) -> None:
        """Quiet, read back, let go - after whatever else happened."""
        self._stop_receiver()
        quiet = False
        if self.chans:
            quiet = self._quiet()
            if not quiet and self._watch_chans:
                quiet = self._quiet(self._watch_chans)
            if not quiet:
                log.error("Pluto %s: the transmitter did not read back quiet at close", self.uri)
        if self.guard is not None:
            # Last, after the quiet above: the guard's own goodbye quiets
            # once more, and until then it was still watching.
            try:
                self.guard.stop()
            except Exception:                       # noqa: BLE001 - closing regardless
                log.exception("stopping the Pluto's transmitter guard")
        self._watch_stop.set()
        if self._watch_thread is not None and self._watch_thread is not threading.current_thread():
            self._watch_thread.join(timeout=1)
        self._watch_thread = None
        self._rxbuf = None
        self._rx_ctx = None
        self.rx_listening.clear()
        self._dds = None
        self.ctx = self._watch_ctx = None
        self.chans = {}
        self._watch_chans = {}

    # -- transmit ----------------------------------------------------------------------

    def _radio_key(self) -> None:
        if self.ctx is None:
            raise LinkRefused("not-open")
        self._tx_error = None
        try:
            self._dds.set_kernel_buffers_count(TX_KERNEL_BUFFERS)
        except (AttributeError, OSError):
            pass
        try:
            buf = self._lib.Buffer(self._dds, self.tx_block, False)
        except OSError as e:
            raise LinkRefused("radio-refused", f"Pluto {self.uri}: the transmit buffer ({e})") from None
        # The order is the proof's: the buffer made (the DAC fed zeros),
        # then the gain, then the LO up, then samples. The watchdog counts
        # from before the LO write, so a write that hangs is counted too.
        if not self.rx_during_tx:
            self._rx_go.clear()                     # the receiver stops reading (see _read_iq)
        if self._gpo_bit:
            # The relay first, and given its lead, before any RF.
            if not self._gpo_set(True):
                raise LinkRefused("radio-refused", f"Pluto {self.uri}: the PTT output did not come on")
            self._tx_on_since = time.monotonic()
            time.sleep(self.relay_lead_s)
        if self.ptt_output == "firmware":
            # The firmware's GPO follows the LO or the samples: both come up
            # at the quiet gain, and the over's own gain after the lead
            # (`_lead_gain`, from the writer once samples are flowing).
            self._tx_on_since = time.monotonic()
            self._write("tx_lo", "powerdown", 0)
            self._gain_pending = True
            threading.Thread(target=self._lead_gain, name="PlutoLink relay lead", daemon=True).start()
        else:
            self._write("tx", "hardwaregain", self.tx_gain_db)
            if self._tx_on_since is None or not self._gpo_bit:
                self._tx_on_since = time.monotonic()
            self._write("tx_lo", "powerdown", 0)
        self._txq = queue.Queue()
        self._tx_queued = 0
        self._tx_until = time.monotonic()
        self._first_push = True
        self._lead_reached = False
        self.stats["tx_gaps"] += 0                  # present in stats from the first over, 0 or not
        self.stats["tx_late"] += 0
        stop = threading.Event()
        self._tx_stop = stop
        self._tx_thread = threading.Thread(target=self._transmit, args=(buf, self._txq, stop),
                                           name="PlutoLink transmit", daemon=True)
        self._tx_thread.start()

    def _radio_audio(self, pcm: bytes) -> bool:
        if self._tx_error is not None or self._tx_stop is None:
            return False
        audio = np.frombuffer(pcm, "<i2").astype(np.float64) / 32768.0
        self._tx_queued += len(audio)
        self._txq.put(audio)
        return True

    def _radio_audio_abort(self) -> None:
        q = self._txq
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
        self._tx_queued = 0

    def _radio_drain_s(self) -> float:
        """How long after the base class's own estimate the last audio still
        plays. The base waits `_written_until - now` (the audio it handed
        over ahead of real time) *plus* this; until 18/10/2026 this returned
        everything still to play, so the lead was counted twice and every
        over ran on as carrier for about LEAD_S (0.071 s on air at 150 ms;
        0.285 s in the tests at 250 ms). Now: when the last engine audio
        will have played by the writer's model (`_audio_end_at`, or the
        queue not yet pushed beyond `_tx_until`), plus TX_LATENCY_S for the
        DMA and DAC, less what the base already waits."""
        now = time.monotonic()
        if self._tx_queued:
            end = max(self._tx_until, now) + self._tx_queued / self.rate
        else:
            end = self._audio_end_at if self._audio_end_at is not None else max(self._tx_until, now)
        base_waits = max(0.0, self._written_until - now)
        return max(0.0, end + TX_LATENCY_S - now - base_waits)

    def _radio_alive(self) -> bool:
        # A writer that failed has already quieted the radio; raising here
        # (rather than answering False, which the base reads as the radio
        # gone away) makes the supervisor abort the over as radio-error.
        err = self._tx_error
        if err is not None:
            raise OSError(f"the Pluto's transmit stream failed: {err}")
        return True

    def _radio_unkey(self) -> None:
        # Quiet first: the RF goes off at once, whatever the writer is doing.
        asked = time.monotonic()
        if self.ptt_output == "firmware" and self.chans:
            # The gain down now, the LO (and with it the firmware's GPO)
            # after the tail.
            try:
                self._write("tx", "hardwaregain", TX_ATTEN_MAX)
            except (SdrError, PacketError):
                pass
            time.sleep(self.relay_tail_s)
        quiet = self._quiet_rf() if self.chans else False
        off = time.monotonic()
        if self._gpo_bit and self.chans:
            # The RF is off; the relay drops after its tail.
            time.sleep(self.relay_tail_s)
            if not self._gpo_set(False):
                quiet = False
        if quiet and self._audio_end_at is not None:
            self._note_unkey(asked, off)
        self._rx_go.set()                           # listening again the moment it is quiet
        self._stop_writer()
        if not quiet:
            raise OSError("the Pluto's transmitter did not read back quiet")

    def _note_push(self, pushing: float, now: float, audio: bool = True) -> None:
        """Whether this push reached the Pluto in time (stats, `timing`):
        `tx_late` counts pushes that returned after everything pushed
        before them should already have played - by the model, the DAC ran
        dry for `now - _tx_until` and sent a hole of LO leakage, not the
        channel - and `tx_late_ms` adds those holes up. The first push of
        an over has nothing before it and is not counted. `tx_push_ms_max`
        is the longest a push took (iiod and the USB network), and
        `tx_slack_min_ms` the least queued ahead when a push of engine
        audio returned, once the queue had first filled to most of LEAD_S
        (the first pushes of an over start from nothing, and only ever
        carry preamble)."""
        st = self.stats
        took = now - pushing
        st["tx_push_ms_max"] = max(st["tx_push_ms_max"], round(took * 1000, 1))
        first, self._first_push = self._first_push, False
        if first:
            return
        slack = self._tx_until - now
        if slack >= 0.8 * self.LEAD_S:
            self._lead_reached = True
        if audio and self._lead_reached:
            st["tx_slack_min_ms"] = round(min(st.get("tx_slack_min_ms", slack * 1000), slack * 1000), 1)
        if slack < 0:
            st["tx_late"] += 1
            st["tx_late_ms"] = round(st["tx_late_ms"] - slack * 1000, 1)

    def _note_unkey(self, asked: float, off: float) -> None:
        """What `timing` reports about this over's end (see there)."""
        tail = off - self._audio_end_at
        self._audio_end_at = None
        self._timing.update(unkey_tail_s=round(tail, 4), quiet_write_s=round(off - asked, 4))
        self.stats["overs_timed"] += 1
        self._timing["unkey_tail_max_s"] = round(max(tail, self._timing.get("unkey_tail_max_s", tail)), 4)
        self._rx_wait_since = off

    def timing(self) -> dict:
        """The turnaround as this link sees it, for a harness and the
        diagnostics (PLAN-0.2 R4):

        - `turnaround_s`: the key-up time the channel allows for (measured
          or assumed, base.py);
        - `unkey_tail_s` (and its largest, `unkey_tail_max_s`): from when
          the last sample of the over should have played - by the writer's
          own model of the DMA queue, `_tx_until` - to the TX LO powerdown
          reading back. Positive is carrier after the audio (the drain's
          allowance plus the write's round trip); negative would mean the
          end of the audio was cut. A model, not a measurement of RF: the
          keyed session checks it against the other station's S-meter;
        - `quiet_write_s`: the powerdown and gain writes and their read-back
          alone, i.e. how long "unkey" takes to reach the chip;
        - `rx_after_unkey_s`: from that read-back to the next receive block
          arriving - the receiver is never blanked during or after an over,
          so this is only the block size and libiio's queue;
        - `rx_block_s`, `rx_queue_s`: that block, and the most the kernel's
          receive queue can hold behind it (stale, never dropped: a reply
          that starts while it drains is heard late, not lost)."""
        out = dict(self._timing)
        out.update(turnaround_s=self.turnaround_s, rx_block_s=RX_BLOCK_S, rx_queue_s=RX_BLOCK_S * RX_KERNEL_BUFFERS)
        return out

    def _stop_writer(self) -> None:
        stop, thread = self._tx_stop, self._tx_thread
        self._tx_stop = self._tx_thread = None
        if stop is not None:
            stop.set()
            self._txq.put(None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
            if thread.is_alive():
                # Held inside a push; it exits when that returns, and the
                # buffer goes with it. The transmitter is already quiet.
                log.warning("Pluto %s: the transmit writer is stuck in a push", self.uri)
        self._tx_queued = 0

    def _transmit(self, buf, q: queue.Queue, stop: threading.Event) -> None:
        """The writer: fixed TX_BLOCK buffers, audio when there is some,
        carrier when the DAC would otherwise run dry (module docstring)."""
        piece = int(self.rate * TX_BLOCK_S)
        pend = np.zeros(0)
        had_audio = filled = False
        try:
            while not stop.is_set():
                while len(pend) < piece:
                    wait = self._tx_until - time.monotonic() - CARRIER_MARGIN_S
                    try:
                        got = q.get(timeout=wait) if wait > 0 else q.get_nowait()
                    except queue.Empty:
                        break
                    if got is None:
                        return
                    if filled and had_audio:
                        self.stats["tx_gaps"] += 1
                    had_audio, filled = True, False
                    self._tx_queued = max(0, self._tx_queued - len(got))
                    pend = np.concatenate([pend, got])
                if stop.is_set():
                    return
                if len(pend) < piece:
                    filled = True
                    fill = piece - len(pend)
                    self.stats["tx_fill_s"] += fill / self.rate
                    block, pend = np.concatenate([pend, np.zeros(fill)]), np.zeros(0)
                else:
                    fill = 0
                    block, pend = pend[:piece], pend[piece:]
                buf.write(bytearray(iq_raw(self._mod(block))))
                pushing = time.monotonic()
                buf.push()
                now = time.monotonic()
                self._note_push(pushing, now, fill < piece)
                self._tx_until = max(now, self._tx_until) + TX_BLOCK_S
                if fill < piece:
                    # Engine audio in this block, ending `fill` samples
                    # (of carrier) before the block does.
                    self._audio_end_at = self._tx_until - fill / self.rate
                self.stats["tx_pushed"] += 1
        except Exception as e:                      # noqa: BLE001 - any failure: quiet, then tell the base
            log.error("Pluto %s: transmit stream failed: %s", self.uri, e)
            if not stop.is_set():
                self._tx_error = e
            if not self._quiet() and self._watch_chans:
                self._quiet(self._watch_chans)

    # -- the unkey confirmed, and the way out without the lock -------------------------

    #: Every unkey is confirmed by reading the transmitter back (base.py, "an
    #: unkey is confirmed"). `_radio_unkey` already reads back once, at the
    #: moment it quiets; the base's later read-back catches the LO coming up
    #: again after that - a writer still finishing a push it was inside, or
    #: anything else that keyed it - and sends the unkey again.
    CONFIRMS_UNKEY = True

    def _radio_ptt_readback(self) -> bool | None:
        """Transmitting means "not quiet": TX LO powered or the gain above
        its quiet value, read through the watchdog's context when there is
        one, so the read does not wait behind a push on the main one. None
        when either cannot be read."""
        chans = self._watch_chans or self.chans
        if not chans:
            return None
        state = self._tx_read(chans)
        if state["powerdown"] is None or state["gain_db"] is None:
            return None
        return not state["quiet"]

    def _emergency_unkey(self) -> None:
        """The base's lock-free unkey (hard watchdog, atexit, a crash
        handler). The default would quiet through the main context, which
        is the thing a push stuck mid-over holds: this goes through the
        watchdog's own context instead, tells the writer to stop without
        waiting for it, and raises if the radio does not read back quiet so
        the base says so and tries again."""
        stop = self._tx_stop
        if stop is not None:
            stop.set()
            # The supervisor, once it runs again, aborts the over as
            # radio-error rather than carry on pacing audio into a writer
            # that has been stopped.
            if self._tx_error is None:
                self._tx_error = OSError("the transmitter was quieted by an emergency unkey")
        chans = self._watch_chans or self.chans
        if chans and not self._quiet(chans):
            raise OSError(f"Pluto {self.uri}: the transmitter did not read back quiet")

    # -- the independent watchdog ---------------------------------------------------

    def _watch(self) -> None:
        said = False
        while not self._watch_stop.wait(WATCH_EVERY_S):
            since = self._tx_on_since
            if since is None:
                said = False
                continue
            held = time.monotonic() - since
            if held <= self.max_key_s + WATCHDOG_GRACE_S:
                continue
            quiet = self._quiet(self._watch_chans or None)
            if quiet:
                if self._tx_error is None:
                    self._tx_error = OSError("the TX watchdog quieted the transmitter")
                log.error("Pluto %s: TX up %.1f s, past the %.1f s limit; the watchdog quieted it",
                          self.uri, held, self.max_key_s)
                # base._event takes no lock: safe while the supervisor is stuck.
                self._event("unkeyed", "watchdog", {"seconds": round(held, 2), "by": "pluto-watchdog"})
            elif not said:
                said = True
                log.error("Pluto %s: TX up %.1f s and the watchdog cannot quiet it; retrying", self.uri, held)

    # -- receive ------------------------------------------------------------------------

    def _read_iq(self) -> None:
        """The IQ reader. Not `_receive`: that name is the base class's
        receive thread, which feeds the engine from `_rx`'s queue - a
        PlutoLink method of the same name replaced it, so two threads read
        the one iio buffer and nothing ever reached the engine.

        **A failed read is not the end of receiving.** Workbench's Tezuka
        nano (18/10/2026, receive-only, nothing keyed) failed a refill with
        EPIPE - iiod closing the stream - 3 to 30 s into a run at 2.4576 and
        at 2.4 MS/s, after reads had been arriving at 55-75 % of real time;
        and the first keyed run with a Pluto that afternoon lost its
        receiver the same way three seconds into an over. A receiver that
        stops hears no acknowledgement again, so the buffer is made again
        (`_rebuild_rx`: the main context, else a fresh one, which on the
        nano is what worked: the old stream holds the device busy for a
        moment) up to RX_REBUILDS times a minute, each said in the log and
        as a radio-error event, and counted (`rx_failures`, `rx_rebuilds`;
        `rx_max_gap_ms` is the longest wait for a block)."""
        buf = self._rxbuf
        fails: list[float] = []
        last = time.monotonic()
        while not self._rx_stop.is_set():
            try:
                if not self._rx_go.is_set():
                    # Our own over (rx_during_tx off): stop reading, so
                    # iiod and the USB network carry the transmit stream
                    # alone. Both directions at 2.4 MS/s are ~19 MB/s,
                    # which is about what this link moves at all (receive
                    # alone at 4.8 MS/s kept 96 % of real time on shack's
                    # Pluto, 18/10/2026). The buffer is let go and made
                    # anew after the over, so what the kernel queued
                    # during it - our own signal, up to RX_KERNEL_BUFFERS
                    # blocks old - is never handed on as if it were now.
                    self._rxbuf = buf = None
                    self.stats["rx_paused"] += 1
                    while not self._rx_go.wait(0.05):
                        if self._rx_stop.is_set():
                            return
                    buf = self._rxbuf = self._lib.Buffer(self._rxdev, self.rx_block, False)
                    last = time.monotonic()
                buf.refill()
                raw = buf.read()
                if self._rx_stop.is_set():
                    break
                now = time.monotonic()
                gap_ms = round((now - last) * 1000, 1)
                last = now
                if gap_ms > self.stats["rx_max_gap_ms"]:
                    self.stats["rx_max_gap_ms"] = gap_ms
                if not self.rx_listening.is_set():
                    self.rx_listening.set()
                since = self._rx_wait_since
                if since is not None:
                    self._rx_wait_since = None
                    self._timing["rx_after_unkey_s"] = round(now - since, 4)
                z = (np.frombuffer(raw, np.int16).astype(np.float32) / FULL_SCALE).view(np.complex64)
                self._hear(z)
            except Exception as e:                  # noqa: BLE001 - said, and the receiver is made again
                if self._rx_stop.is_set():
                    return
                now = time.monotonic()
                fails = [t for t in fails if now - t < 60.0] + [now]
                self.stats["rx_failures"] += 1
                self.rx_listening.clear()
                if self.receiver is not None:
                    self.receiver.busy = None
                log.error("Pluto %s: receive failed after %.2f s without a block (longest wait %.0f ms): %s",
                          self.uri, now - last, self.stats["rx_max_gap_ms"], e)
                self._event("radio", "radio-error", {"what": "receive", "error": str(e)})
                self._rxbuf = buf = None
                if len(fails) > RX_REBUILDS:
                    log.error("Pluto %s: receive failed %d times in a minute; the receiver stops", self.uri,
                              len(fails))
                    return
                buf = self._rebuild_rx()
                if buf is None:
                    log.error("Pluto %s: the receive buffer could not be made again; the receiver stops", self.uri)
                    return
                self._rxbuf = buf
                self.stats["rx_rebuilds"] += 1
                last = time.monotonic()
                log.warning("Pluto %s: receive buffer made again (%d so far); listening", self.uri,
                            self.stats["rx_rebuilds"])

    def _rebuild_rx(self):
        """A new receive buffer after a failed read: on the main context,
        else on a fresh one (kept in `_rx_ctx` as long as its buffer), a few
        tries a second apart, since the stream that failed can hold the
        device busy (EBUSY) for a moment. Receive settings are the
        device's, so a new context needs none written."""
        for attempt in range(RX_REBUILD_TRIES):
            if self._rx_stop.wait(min(RX_REBUILD_MAX_WAIT_S, RX_REBUILD_WAIT_S * (attempt + 1))):
                return None
            for which in ("main", "new"):
                try:
                    ctx = self.ctx if which == "main" else self._lib.Context(self.uri)
                    if ctx is None:
                        continue
                    if which == "new":
                        try:
                            ctx.set_timeout(3000)
                        except (AttributeError, OSError):
                            pass
                    if which == "new" and not self._fresh_context_ok(ctx):
                        return None
                    rxdev = ctx.find_device("cf-ad9361-lpc")
                    for i in (0, 1):
                        rxdev.find_channel(f"voltage{i}", False).enabled = True
                    try:
                        rxdev.set_kernel_buffers_count(RX_KERNEL_BUFFERS)
                    except (AttributeError, OSError):
                        pass
                    buf = self._lib.Buffer(rxdev, self.rx_block, False)
                    self._rxdev = rxdev
                    if which == "new":
                        self._rx_ctx = ctx
                    return buf
                except Exception as e:              # noqa: BLE001 - the next way, or the next try
                    log.warning("Pluto %s: remaking the receive buffer on the %s context (try %d): %s",
                                self.uri, which, attempt + 1, e)
        return None

    def _fresh_context_ok(self, ctx) -> bool:
        """A context opened while the receiver was failing may be to a Pluto
        that has restarted: workbench's nano, in a receive-only soak on
        18/10/2026, froze, was reset by its own watchdog and came back with
        its TX LO up at 2450 MHz, -10 dB, for nearly five minutes until a
        person quieted it (GUIDE §5, "a Pluto reboot keys its TX LO"). So the
        first thing done through any fresh context is the quiet, read back;
        and a Pluto whose receive rate is no longer ours has restarted and
        lost every setting, so the receiver says so and stops rather than
        carry on hearing the wrong frequency. False when it must stop."""
        chans = self._channels(ctx)
        quiet = bool(chans) and self._quiet(chans)
        rate = _number(self._read_attr("rx", "sampling_frequency", chans)) if chans else None
        if rate is not None and abs(rate - self.rate_iq) > 1:
            log.error("Pluto %s has restarted (receive rate %s, not %s): transmitter %s; the receiver stops - "
                      "close and open the radio again", self.uri, rate, self.rate_iq,
                      "quieted" if quiet else "NOT READ BACK QUIET")
            self.stats["radio_restarted"] += 1
            self._event("radio", "radio-error", {"what": "restarted", "tx_quiet": quiet})
            return False
        if not quiet:
            log.error("Pluto %s: a fresh context did not read the transmitter back quiet", self.uri)
        return True

    def listen_modes(self, modes) -> None:
        """The modes to demodulate from now (the engine's, adapt.py)."""
        self._listen = set(modes)
        if isinstance(self.receiver, MultiModeReceiver):
            self.receiver.listen(self._listen)

    def _hear(self, z: np.ndarray) -> None:
        rx = self.receiver
        pcm = rx.hear(z)
        if len(pcm):                                # s16 bytes (AFSK) or a float array (FSK)
            # One mode's receiver sends its level with the audio, for the
            # C/N0 of the frames in it; an adaptive link's bundle carries
            # each mode's in its parts (MultiModeReceiver has no one level).
            peak = getattr(rx, "peak_level_db", None)
            self._rx(pcm, None if peak is None else (peak, rx.noise_bw_hz))

    @property
    def level_db(self) -> float | None:
        """The channel's power over its own floor (dB), averaged as busy is;
        None until the floor is known. What a harness watching for another
        station's carrier reads."""
        return self.receiver.level_db if self.receiver is not None else None

    def _stop_receiver(self) -> None:
        self._rx_stop.set()
        thread, self._rx_thread = self._rx_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _radio_busy_now(self) -> bool | None:
        return self.receiver.busy if self.receiver is not None else None
