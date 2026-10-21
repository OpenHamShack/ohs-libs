"""The receive audio on a USB cable: the codec's own rate in, 12 kHz out.

The sibling of `icomlan.py`'s `AudioStream`, and it has the easier half of the
job. There is no session to keep alive, no sequence number and no retransmit
buffer; a sound card either hands over samples or it does not. What it has
instead is the thing the LAN path never had to think about, because on the LAN
the radio is *told* what to send: **a sound card sends what it sends, and the
only honest thing to do is ask it first.**

**Capture native, decimate here.** This is the whole of the file and it was
paid for in one evening's measurements, written down in `docs/OFF-AIR.md`. The
obvious command - `arecord -r 12000`, the rate the decoders read - does not
produce 12 kHz audio on the IC-7610's codec. The codec captures at 48 kHz and
nothing else, so ALSA's `plug` layer resamples, and measured against real
signals it gets that wrong by about 2.1%: every frequency comes out 2.1% high
and the same 2.5 minutes of 20 m gave **6 decodes as captured against 51 with
the error corrected**. Nothing about it looks broken. The levels are right, the
decoders run, FT8 decodes, and every reported frequency is tens of hertz out in
a log nobody re-reads.

So the rate is never asked for. The card is asked what it can do, the highest
rate it offers that is an exact multiple of 12000 is taken, and the division is
done here by taking every Nth sample - which is exact, needs no correction, and
is the same arithmetic `slicer.resample_to_12k`'s docstring already says is
exact for the 4:1 case. A card that cannot offer an exact multiple is refused
with a sentence saying so, rather than resampled and quietly wrong.

**A radio can be two receivers.** The IC-7610's capture interface is two
channels, and its ACC/USB output menu can put Main on one and Sub on the other.
It was set that way when this was measured, and the two were proved to be
independent off air: left gave FT8 from Belize on 14.074, right gave none, one
sampled pair in 2475 was identical between them and the RMS difference between
the channels was larger than either channel. **One radio, one antenna, two
receive streams.** Every capture before that used `-c1` and threw one of them
away.

So every channel the card offers becomes a `Receiver` with a name, and the
audio is delivered with the name of the receiver it came from. Only one of them
is decoded today - see `icomusb.py` - but nothing here has one channel baked
into it, and the extra channel costs one array slice per block.

**Which channel is Main and which is Sub is not established here, and the
module does not need it to be.** Icom document the ACC/USB output as Main on
the left and Sub on the right, and that is what the labels below say and what
they are marked as: a reading of a manual. The evidence here points the other
way and is not strong enough to overturn it - `docs/OFF-AIR.md` read the CI-V
as saying the selected VFO was the Sub, and it was the selected VFO's band that
came out of the left channel.

What *is* established, twice, against the real radio, is the thing the module
actually depends on: **the left channel carries the VFO that this module's own
`set_freq` acts on.** Tuned to 14.074 and to 7.074 on two separate runs, the
left channel decoded the FT8 on that dial and the right channel decoded none of
it. That is why the first channel is the primary receiver and why the decoder
that does not ask for one gets it - not because left is Main, which nobody
here has proved.

So the keys are `left` and `right`, which is what the hardware says and what
was measured. Nothing downstream decides anything on the Main/Sub label.

**`arecord` rather than an ALSA binding.** `icomusb.py` wrote this decision
down before there was anything to run: a process that dies can be restarted,
where a wedged C extension in the audio path of a month-long run takes the
module with it. It is also the same choice the module already makes for `jt9`,
`wsprd` and `js8`, it adds no dependency to an image whose whole argument is
that it installs four packages, and `alsa-utils` is a 1 MB package that is on
every host this runs on. The cost is one pipe and one copy per block, measured
below the noise of the decimation itself.

**What it costs, because this is the first thing in the module that runs
continuously.** A capture that never stops is different in kind from a decode
that runs for two seconds in fifteen. Measured on `shack` against the real
7610, 48 kHz stereo, 50 ms blocks: see `docs/USB-AUDIO.md`. The arithmetic here
is deliberately all slicing - `array` extended slices are a C loop, not a
Python one - because the same work written as a `for` over samples was the one
shape that could not fit on a Pi 400.
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .i18n import _

log = logging.getLogger("shack_rig.usbaudio")

#: What the WSJT-X decoders read, and therefore what everything downstream of
#: this file is entitled to assume it is being handed.
DECODE_RATE = 12000

#: The highest rate worth capturing at. Above this is USB bandwidth and
#: decimation work spent on audio a receiver's own filters removed long before
#: the codec saw it - the radio's AF output is a few kilohertz wide. 48 kHz is
#: also the one rate every USB codec in a radio actually has.
MAX_RATE = 48000

#: How much audio is carried in one hand-over, in seconds.
#:
#: It decides two things that pull in opposite directions. `SlotBuffer` stamps
#: samples with the moment the block reached it, so a block is attributed up to
#: its own length late, and the decoders are measurably fussy about where a
#: period is cut - the same period cut 50 ms differently decodes three signals
#: or none. And every block is a wakeup, a read and a slice, so a short one is
#: work on a machine that has none to spare.
#:
#: 50 ms puts the attribution error inside the 0.4 s of slack the live runs
#: already measured (`docs/OFF-AIR.md` reports dt between -0.4 and +0.8 s) and
#: costs 20 wakeups a second, which is a twelfth of what the CI-V poll loop
#: already costs. It is not worth tuning more finely than that without a
#: measurement that says so.
BLOCK_SECONDS = 0.05

#: The ALSA buffer behind that. Four blocks: enough that a scheduling hiccup on
#: a loaded Pi does not become an overrun, short enough that the lag between a
#: sample being heard and being attributed to a period stays small. An overrun
#: is not fatal - arecord carries on - but it is a hole in a period, and a hole
#: is a decode that does not happen.
BUFFER_BLOCKS = 4

#: Restarting after the capture dies. A radio switched off at the wall takes
#: its sound card with it, exactly as it takes its serial port, and that is the
#: ordinary case rather than a failure - so it is retried for as long as the
#: link is up, with the doubling backoff `Radio._supervise` uses for the same
#: reason.
#:
#: The first wait is a quarter of a second rather than the supervisor's one
#: second, because the two are recovering from different things. A radio that
#: is switched off will be switched off for minutes; a capture that failed to
#: start may have failed transiently. That happened once in five runs against
#: the real 7610 - `arecord` refused with "Unable to install hw params" on a
#: set of parameters the card had just said it accepted, and the same
#: parameters worked immediately afterwards - and a transient that costs a
#: second and a half of a 15 second period is a period that decodes worse for
#: no reason. Doubling from here still reaches the ceiling inside a minute.
BACKOFF_START = 0.25
BACKOFF_MAX = 30.0

#: How many lines of `arecord`'s complaint to keep. Its useful errors are one
#: line ("Device or resource busy", "Sample format non available") and it
#: follows them with a list of what it would have accepted, which is the part a
#: person reading this from another building actually needs.
STDERR_LINES = 6

#: Levels are measured on one sample in this many. A level meter wants to know
#: whether a receiver is hearing anything, not the sample-accurate RMS of it,
#: and the difference between the two is a Python loop over 12000 samples a
#: second per channel on a machine that cannot afford one.
LEVEL_STRIDE = 16

#: Where a sound card's stable name lives, the way `/dev/serial/by-id` is where
#: a serial port's does. `hw:2,0` is an enumeration order: plug a webcam in
#: before the radio on the next boot and the profile points at the webcam.
SND_BY_ID = "/dev/snd/by-id"


class AudioError(Exception):
    """Something about the sound card that a person can act on."""


# -- what the card is, and what it will do ----------------------------------


@dataclass(frozen=True)
class Card:
    """One line of /proc/asound/cards."""

    index: int
    ident: str          #: the short id, "CODEC" - stable per card, not per host
    driver: str         #: "USB-Audio", "HDA-Intel"
    name: str

    def public(self) -> dict:
        return {"index": self.index, "id": self.ident,
                "driver": self.driver, "name": self.name}


@dataclass(frozen=True)
class Receiver:
    """One channel of the capture, which on a radio is one receiver.

    `key` is what the rest of the module keys off, and it names the *channel*
    rather than the receiver, because the channel is what was measured and the
    receiver is what Icom's manual says is on it. `label` carries the reading
    of the manual, and `inferred` says that it is one.
    """

    key: str
    channel: int
    label: str
    inferred: bool = False

    def public(self) -> dict:
        return {"key": self.key, "channel": self.channel,
                "label": self.label, "inferred": self.inferred}


#: What each channel of a two-channel capture is called.
#:
#: Icom's ACC/USB AF output menu offers Main, Sub, or both split left and
#: right, and documents the split as Main on the left. That reading is here and
#: is marked inferred, because the one measurement this module has points the
#: other way - see the header. Nothing decides anything on these strings.
STEREO = (
    Receiver("left", 0, "Left channel - Main, per the radio's manual", True),
    Receiver("right", 1, "Right channel - Sub, per the radio's manual", True),
)

#: And when the card gives one channel, which is what a mono capture interface
#: or a radio set to Main-only produces. No inference to make: there is one
#: stream and it is whichever receiver the radio was told to send.
MONO = (Receiver("mono", 0, "The radio's audio output", False),)


@dataclass
class Format:
    """The capture this module is going to ask for, and where it came from."""

    device: str         #: the ALSA name handed to arecord, e.g. "hw:2,0"
    rate: int
    channels: int
    #: In words, for the state payload: which card was picked and why, and
    #: what it said it could do. The transcript rather than the conclusion,
    #: for the reason `usbprobe.py` gives at length.
    how: str = ""

    @property
    def decimation(self) -> int:
        return self.rate // DECODE_RATE

    @property
    def frame_bytes(self) -> int:
        return self.channels * 2

    def receivers(self) -> tuple[Receiver, ...]:
        return STEREO if self.channels == 2 else MONO

    def block_bytes(self) -> int:
        """Bytes in one hand-over, rounded to a whole decimation group.

        Rounded here rather than left to `Split` to absorb, because a block
        that is a whole number of groups leaves no remainder to carry and the
        common path is then one slice and no copy.
        """
        group = self.decimation * self.frame_bytes
        want = int(self.rate * BLOCK_SECONDS) * self.frame_bytes
        return max(group, (want // group) * group)

    def public(self) -> dict:
        return {"device": self.device, "rate": self.rate,
                "channels": self.channels, "decimation": self.decimation,
                "how": self.how,
                "receivers": [r.public() for r in self.receivers()]}


def cards(proc: str = "/proc/asound/cards") -> list[Card]:
    """Every sound card the kernel has, as the kernel lists them.

    Parsed rather than shelled out for: `aplay -l` is the same information
    behind a program that has to be installed and whose output has changed
    shape before now. This file has not since ALSA 1.0.
    """
    try:
        text = Path(proc).read_text()
    except OSError:
        return []
    out: list[Card] = []
    line_re = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(\S+)\s*-\s*(.*)$")
    for line in text.splitlines():
        m = line_re.match(line)
        if m:
            out.append(Card(int(m.group(1)), m.group(2).strip(),
                            m.group(3), m.group(4).strip()))
    return out


def card_of_control(path: str) -> int | None:
    """The card index behind a /dev/snd path, following it if it is a link.

    `/dev/snd/by-id/...` is a symlink to a `controlCN`, which is the *control*
    device rather than the PCM one - so a by-id path cannot be handed to
    `arecord` directly and has to become a card number first. That number is
    an enumeration order and is exactly what must not be stored; resolving it
    at the moment of use is the whole point of storing the link instead.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    m = re.search(r"/(?:control|pcm)C(\d+)", real)
    return int(m.group(1)) if m else None


def find_card(want: str = "", found: list[Card] | None = None,
              by_id: str = SND_BY_ID) -> tuple[str, str]:
    """Which ALSA device to capture from, and the sentence explaining it.

    Three shapes are accepted and the order matters:

    * A path under `/dev/snd` - the by-id link, which is what a profile should
      hold - is followed to a card number and becomes `hw:N,0`.
    * Anything else with a colon or a recognised prefix is an ALSA name and is
      passed through untouched. Somebody who types `plughw:CARD=CODEC,DEV=0`
      has asked for the plug layer and gets it, including its resampling; that
      is their business and it is not second-guessed here.
    * Empty means find it, which is the case the Raspberry Pi appliance is
      built around: nobody is going to read a card number off a headless box.

    Finding it means *one* candidate, not the best of several. A host with two
    USB codecs on it cannot be guessed at, and picking one would produce a
    module that decodes the wrong radio and says nothing - so it refuses and
    names both, which is a message somebody can act on.
    """
    if want.startswith("/dev/snd") or want.startswith(by_id):
        index = card_of_control(want)
        if index is None:
            raise AudioError(_(
                "{device} does not lead to a sound card. The device may not be plugged in, or may not be "
                "assigned to this module - Dock360's module page, Devices.").format(device=want))
        return f"hw:{index},0", f"{want} is card {index}"
    if want:
        return want, f"{want}, as given"

    have = cards() if found is None else found
    usb = [c for c in have if c.driver == "USB-Audio"]
    if not usb:
        others = ", ".join(f"{c.index} {c.ident}" for c in have) or "none at all"
        raise AudioError(_(
            "no USB sound card is visible; the cards here are {others}. A radio's codec appears as a USB-Audio "
            "card when it is plugged in and switched on, and the device has to be assigned to this module - "
            "Dock360's module page, Devices.").format(others=others))
    if len(usb) > 1:
        names = "; ".join(f"card {c.index} [{c.ident}] {c.name}" for c in usb)
        raise AudioError(_(
            "there are {n} USB sound cards here and nothing says which is the radio: {names}. Put one of their "
            "/dev/snd/by-id/... paths in the profile's audio device.").format(n=len(usb), names=names))
    c = usb[0]
    return f"hw:{c.index},0", f"card {c.index} [{c.ident}] {c.name}, the only USB sound card here"


_RANGE = re.compile(r"^(\w+):\s*[\[(]?\s*(\d+)(?:\s+(\d+))?\s*[\])]?\s*$")


def parse_hw_params(text: str) -> dict[str, tuple[int, int]]:
    """`arecord --dump-hw-params` as ranges, for the two lines that matter.

    ALSA merges every altsetting of the interface into one interval, so the
    7610's codec - which offers 48000 on one altsetting and 8000 on another -
    reports `RATE: [8000 48000]` and no way to tell that 12000 is not among
    them. That is not a problem as long as nothing believes the middle of the
    range: what is wanted is the *top*, and the top of a merged interval is a
    rate some altsetting really has.
    """
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        m = _RANGE.match(line.strip())
        if not m:
            continue
        lo = int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else lo
        out[m.group(1)] = (lo, hi)
    return out


def choose(params: dict[str, tuple[int, int]]) -> tuple[int, int]:
    """The rate and channel count to ask for. Raises if there is no honest one.

    The rate is the highest exact multiple of 12000 the card will do, capped:
    exact so that decimating is taking every Nth sample and not resampling,
    highest so that nothing is thrown away before this module has seen it, and
    capped because 96 or 192 kHz from a radio is bandwidth spent on the
    codec's own noise floor.

    The channels are all of them, because on a radio a channel is a receiver
    and the module has thrown one of them away for its whole life so far.
    """
    lo_r, hi_r = params.get("RATE", (0, 0))
    rate = min(hi_r, MAX_RATE)
    rate -= rate % DECODE_RATE
    if rate < lo_r or rate <= 0:
        raise AudioError(_(
            "this sound card offers {lo}-{hi} Hz and none of it is a whole multiple of {rate}. Capturing at a "
            "rate the hardware has not got makes ALSA resample, which was measured on this module's own radio "
            "as a 2.1% frequency error and about an eighth of the decodes - so it is refused rather than done "
            "quietly.").format(lo=lo_r, hi=hi_r, rate=DECODE_RATE))
    lo_c, hi_c = params.get("CHANNELS", (1, 1))
    return rate, max(1, min(hi_c, 2))


async def hw_params(device: str, arecord: str = "arecord") -> str:
    """Ask `arecord` what the device will do, in its own words.

    Asked of the program that is going to do the capture rather than read out
    of `/proc/asound/cardN/stream0`, which only exists for USB cards. It costs
    one subprocess once per connection, next to a CI-V probe that is allowed
    twenty seconds.
    """
    proc = await asyncio.create_subprocess_exec(
        arecord, "-D", device, "--dump-hw-params", "-d", "1", "/dev/null",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    return err.decode("utf-8", "replace")


# -- turning what the card sends into what the decoders read ----------------


class Split:
    """Interleaved frames in; one 12 kHz mono stream per channel out.

    Deinterleaving and decimating are one operation here rather than two,
    because as an extended slice they are the same slice: with `C` channels
    and a decimation of `N`, channel `c` at 12 kHz is every `C*N`th sample
    starting at `c`. `array` does that in C. Written as a loop over samples -
    which is what `slicer.resample_to_12k` does, correctly, for a stream that
    is already mono and usually already 12 kHz - it is a Python iteration
    100,000 times a second on a machine with four slow cores.

    **The remainder is the whole of the state, and it is the part that breaks
    silently.** A read from a pipe ends wherever the pipe felt like ending it,
    which is not a multiple of four frames, and a slice that starts at sample 0
    of every block regardless is a decimation whose phase jumps at every block
    boundary. That is not silence and it is not an exception: it is audio that
    sounds perfectly ordinary and decodes slightly worse, for ever. So whole
    groups are consumed and the tail is carried into the next block.
    """

    def __init__(self, channels: int, rate_in: int):
        if rate_in % DECODE_RATE:
            raise AudioError(_("{rate_in} Hz does not divide into {rate}").format(rate_in=rate_in, rate=DECODE_RATE))
        self.channels = channels
        self.step = rate_in // DECODE_RATE
        #: One output sample per channel comes from this many bytes of input.
        self.group = self.step * channels * 2
        #: How far apart, in samples, two consecutive kept samples of one
        #: channel are in the interleaved stream.
        self.stride = self.step * channels
        self._rest = b""
        self.carried = 0

    def feed(self, data: bytes) -> list[bytes]:
        """One channel's worth of 12 kHz 16-bit PCM per channel, in order."""
        if self._rest:
            data = self._rest + data
        n = (len(data) // self.group) * self.group
        self._rest = data[n:]
        self.carried = len(self._rest)
        if not n:
            return [b""] * self.channels
        whole = array.array("h")
        whole.frombytes(data[:n] if n != len(data) else data)
        if whole.itemsize != 2:                              # pragma: no cover
            raise AudioError(_("this Python's array('h') is not 16-bit"))
        out = []
        for ch in range(self.channels):
            out.append(whole[ch::self.stride].tobytes())
        return out


def level(pcm: bytes, stride: int = LEVEL_STRIDE) -> tuple[int, int]:
    """Peak and a sampled RMS, as whole numbers of a 32768 full scale.

    The peak is exact and free - `min` and `max` over an `array` are a C loop.
    The RMS is taken from one sample in `stride` because it is the only part
    that costs a Python iteration, and what it is for is telling "this
    receiver is hearing a band" from "this cable is not connected". Nothing
    decides anything on a number this rough; `docs/OFF-AIR.md` quotes a healthy
    receiver at an RMS of 900-1800 and peaks near 6000, which is the range it
    exists to put a page's reading beside.
    """
    if not pcm:
        return 0, 0
    a = array.array("h")
    a.frombytes(pcm[: (len(pcm) // 2) * 2])
    if not a:
        return 0, 0
    peak = max(abs(min(a)), abs(max(a)))
    few = a[::stride]
    rms = int((sum(s * s for s in few) / len(few)) ** 0.5)
    return peak, rms


class Levels:
    """What each receiver is hearing, and whether they are the same thing.

    The second question is the one worth having and it is why this is a class
    rather than two numbers. A 7610 whose ACC/USB output is set to Main-only
    still presents a two-channel capture interface - it simply puts the same
    audio on both - and a module that assumed two receivers because it counted
    two channels would offer a second decoder for a stream that is a copy of
    the first. Neither the menu setting nor the radio's own opinion of it is
    readable over CI-V, so the only way to know is to look at the samples.

    Counted on a subsample, and reported as a proportion rather than a verdict,
    because "the same" is a matter of degree: two receivers on the same antenna
    listening to the same band are *correlated* and not identical, and the
    measurement that settled it off air was that 1 sampled pair in 2475 was
    equal. A copy is equal in every pair.
    """

    def __init__(self, receivers: tuple[Receiver, ...]):
        self.peak = {r.key: 0 for r in receivers}
        self.rms = {r.key: 0 for r in receivers}
        self.compared = 0
        self.identical = 0

    def add(self, key: str, pcm: bytes) -> None:
        peak, rms = level(pcm)
        self.peak[key] = peak
        self.rms[key] = rms

    def compare(self, a: bytes, b: bytes, stride: int = LEVEL_STRIDE) -> None:
        n = min(len(a), len(b)) // 2
        if not n:
            return
        left = array.array("h")
        left.frombytes(a[: n * 2])
        right = array.array("h")
        right.frombytes(b[: n * 2])
        few_l = left[::stride]
        few_r = right[::stride]
        self.compared += len(few_l)
        self.identical += sum(1 for x, y in zip(few_l, few_r) if x == y)

    def same_audio(self) -> bool | None:
        """Whether both channels are carrying one receiver, or None yet.

        A threshold rather than equality, because a sample that happens to
        match is ordinary near the noise floor: the off-air measurement of two
        genuinely different receivers was 1 pair in 2475, which is 0.04%, and a
        copy is 100%. Anything above nine tenths is a copy; the gap between the
        two cases is three orders of magnitude wide and nothing lives in it.
        """
        if self.compared < 1000:
            return None
        return self.identical / self.compared > 0.9

    def public(self) -> dict:
        return {"peak": dict(self.peak), "rms": dict(self.rms),
                "same_audio": self.same_audio(),
                "pairs_compared": self.compared}


# -- the capture itself ------------------------------------------------------


class UsbAudio:
    """One `arecord` on one sound card, for as long as the radio is there.

    Owned by `UsbLink`, started and stopped with it. Everything about which
    card, at what rate and in how many channels is settled in `start()` and
    written into `report`, because on an appliance the interesting failure is
    not an exception - it is a module that came up, found a card, and is
    capturing the wrong one.
    """

    def __init__(self, device: str = "", on_pcm=None,
                 arecord: str = "arecord"):
        self.device = device
        #: Called with (receiver key, 12 kHz 16-bit mono PCM) for every block
        #: of every channel. Handed on rather than buffered, for the reason
        #: `Radio._on_audio` gives: what wants it decides how much to keep.
        self.on_pcm = on_pcm
        self._arecord = arecord

        self.format: Format | None = None
        self.error: str | None = None
        self.running = False
        self.blocks = 0
        self.bytes = 0
        self.restarts = 0
        self.overruns = 0
        self.levels: Levels | None = None
        self.stderr: list[str] = []

        self._split: Split | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._draining: asyncio.Task | None = None
        self._wanted = False

    def receivers(self) -> tuple[Receiver, ...]:
        """What this radio is currently delivering, or nothing before it is up.

        Empty rather than a guess before the card has been asked: how many
        receivers a radio is sending is a fact about the cable and the radio's
        own menu, and guessing two on a 7610 would put a second decoder in
        front of somebody whose radio is set to Main-only.
        """
        return self.format.receivers() if self.format else ()

    def primary(self) -> str:
        """The receiver everything that wants one stream gets.

        The first channel. Named as a decision rather than left implicit
        because the day there are two decoders this is the one that keeps the
        behaviour anything already running had.
        """
        got = self.receivers()
        return got[0].key if got else ""

    async def start(self) -> None:
        """Find the card, ask it what it does, and start reading it.

        Errors here are reported rather than raised. A radio with a working
        CI-V cable and no sound card assigned is a module that should control
        the radio and say plainly that it cannot hear it - not one that fails
        to connect at all.
        """
        self._wanted = True
        self.error = None
        try:
            device, how = find_card(self.device)
        except AudioError as e:
            self.error = str(e)
            log.warning("no receive audio: %s", self.error)
            return
        if shutil.which(self._arecord) is None:
            self.error = (f"{self._arecord} is not installed, so there is no way "
                          f"to read the radio's sound card. It is in alsa-utils.")
            log.warning("no receive audio: %s", self.error)
            return
        try:
            said = await hw_params(device, self._arecord)
            rate, channels = choose(parse_hw_params(said))
        except AudioError as e:
            self.error = str(e)
            log.warning("no receive audio: %s", self.error)
            return
        except OSError as e:                                 # pragma: no cover
            self.error = f"could not ask {device} what it can do: {e}"
            log.warning("no receive audio: %s", self.error)
            return

        self.format = Format(device, rate, channels, how)
        self._split = Split(channels, rate)
        self.levels = Levels(self.format.receivers())
        log.info("receive audio: %s at %d Hz in %d channel(s), decimating %d:1 "
                 "to %d Hz - %s", device, rate, channels,
                 self.format.decimation, DECODE_RATE, how)
        self._reader = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop wanting audio, and make sure nothing is left holding the card.

        The failure this exists to prevent is the one `docs/TRANSPORTS.md`
        named before any of it was written: an `arecord` left running holds the
        capture device, so the next connection finds it busy and the radio
        appears to have no audio at all. It is the sound-card version of a
        serial port left locked, and it lasts as long as the process does.
        """
        self._wanted = False
        self.running = False
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        drain, self._draining = self._draining, None
        if drain is not None:
            drain.cancel()
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:                           # pragma: no cover
            return
        try:
            await asyncio.wait_for(proc.wait(), 2.0)
        except asyncio.TimeoutError:                         # pragma: no cover
            log.warning("arecord did not stop when asked; killing it")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    def _argv(self) -> list[str]:
        """Exactly what is asked of ALSA, and every part of it is a decision.

        `hw:` and not `plughw:`. The plug layer is what silently resampled 48
        to 12 and put every frequency 2.1% out; asking the hardware for
        something it has not got must fail loudly and name what it has, which
        is what `hw:` does. Every value below was read off the card a moment
        ago, so this cannot fail on a card that answered.

        `-t raw` because the point of a header is to say the rate and the
        channel count to a program that does not know them, and the program on
        the other end of this pipe is the one that chose them.
        """
        fmt = self.format
        assert fmt is not None
        period = int(fmt.rate * BLOCK_SECONDS)
        return [
            self._arecord, "-D", fmt.device, "-t", "raw",
            "-f", "S16_LE", "-c", str(fmt.channels), "-r", str(fmt.rate),
            "--period-size", str(period),
            "--buffer-size", str(period * BUFFER_BLOCKS),
            "-q",
        ]

    async def _run(self) -> None:
        """Keep a capture going for as long as one is wanted.

        Restarted rather than given up on, with the same backoff
        `Radio._supervise` uses on the link itself and for the same reason: a
        radio switched off at the wall takes its sound card away, and comes
        back with it half an hour later with nobody present to press anything.
        """
        backoff = BACKOFF_START
        while self._wanted:
            try:
                ended = await self._capture_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # pragma: no cover
                log.exception("reading the radio's audio")
                self.error = str(e)
                ended = True
            if not self._wanted:
                return
            if ended:
                self.restarts += 1
                log.warning("the audio capture stopped (%s); trying again in "
                            "%.0fs", self.error or "no reason given", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            else:
                backoff = BACKOFF_START

    async def _capture_once(self) -> bool:
        fmt = self.format
        assert fmt is not None and self._split is not None
        argv = self._argv()
        log.debug("%s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            self.error = f"could not start {self._arecord}: {e}"
            self.running = False
            return True
        self._proc = proc
        self.stderr = []
        # Drained in a task of its own. arecord writes an overrun warning per
        # overrun, and a pipe nobody reads is a pipe that fills - at which
        # point arecord blocks on stderr and stops delivering audio, which
        # presents as a radio that went quiet under load and is exactly the
        # condition the warnings were about.
        self._draining = asyncio.create_task(self._drain(proc))
        self.running = True
        self.error = None
        want = fmt.block_bytes()
        try:
            while True:
                block = await proc.stdout.read(want)
                if not block:
                    break
                self.blocks += 1
                self.bytes += len(block)
                self._deliver(self._split.feed(block))
        finally:
            self.running = False
            rc = proc.returncode
            if rc is None:
                await self._kill()
            elif rc != 0:
                self.error = (self.stderr[-1] if self.stderr
                              else f"{self._arecord} exited {rc}")
            else:
                self.error = f"{self._arecord} stopped on its own"
        return True

    async def _drain(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            said = line.decode("utf-8", "replace").strip()
            if not said:
                continue
            if "overrun" in said.lower():
                # Counted rather than logged each time. An overrun is a hole in
                # a period, so a machine that is dropping them wants to be able
                # to say how many - and a machine dropping one every block must
                # not fill the journal with the news.
                self.overruns += 1
                continue
            self.stderr.append(said)
            del self.stderr[:-STDERR_LINES]
            log.warning("arecord: %s", said)

    def _deliver(self, per_channel: list[bytes]) -> None:
        got = self.receivers()
        levels = self.levels
        if levels is not None and len(per_channel) == 2:
            levels.compare(per_channel[0], per_channel[1])
        for rx, pcm in zip(got, per_channel):
            if not pcm:
                continue
            if levels is not None:
                levels.add(rx.key, pcm)
            if self.on_pcm is not None:
                try:
                    self.on_pcm(rx.key, pcm)
                except Exception:                            # pragma: no cover
                    log.exception("audio listener for %s", rx.key)

    # -- what a person somewhere else needs to see -------------------------

    def stats(self) -> dict:
        """The shape `Radio.audio_stats` publishes, plus what only this knows.

        `packets` and `dropped` are named for the LAN path's counters so that
        one page renders either transport. They are not the same things: a
        block here is a read from a pipe rather than a datagram, and a drop is
        an ALSA overrun rather than a sequence gap. Both mean "audio that
        should have arrived did not", which is the question the page is asking.
        """
        fmt = self.format
        out = {
            "enabled": True,
            "rate": DECODE_RATE if fmt else None,
            "running": self.running,
            "packets": self.blocks,
            "dropped": self.overruns,
            "restarts": self.restarts,
            "error": self.error,
            "device": fmt.device if fmt else self.device,
            "capture": fmt.public() if fmt else None,
            "receivers": [r.public() for r in self.receivers()],
            "primary": self.primary(),
            "arecord": self.stderr[-1] if self.stderr else None,
        }
        if self.levels is not None:
            out["levels"] = self.levels.public()
            same = self.levels.same_audio()
            out["what_the_radio_is_sending"] = _what(self.receivers(), same)
        return out


def _what(receivers: tuple[Receiver, ...], same: bool | None) -> str:
    """One sentence about what is actually coming out of the radio.

    Said rather than assumed, which is the rule this whole file is under. The
    radio's ACC/USB output setting decides whether both receivers are on the
    cable and it cannot be read over CI-V, so the samples are the only evidence
    - and "two channels" is not the same claim as "two receivers".
    """
    if len(receivers) < 2:
        return ("one channel, so one receiver. A radio that can send Main and "
                "Sub separately does it on two channels.")
    if same is None:
        return "two channels; not enough audio yet to say whether they differ."
    if same:
        return ("two channels carrying the same audio, so one receiver. The "
                "radio's ACC/USB AF output is set to Main or to Sub rather "
                "than to both.")
    return ("two channels carrying different audio: this radio is sending two "
            "receivers down one cable.")
