"""Channel access: when this station may transmit, and the radio it keys.

Half-duplex CSMA as every KISS TNC does it (the KISS specification's
p-persistence, which direwolf follows): while the channel is busy, wait;
once it is clear, draw a random number in 0-255 each SLOTTIME and transmit
when it is at most PERSIST, so that two stations waiting on the same
transmission do not both start the instant it ends. A transmission is
TXDELAY of flags (the far receiver's squelch and clock, and this radio's
power coming up), the frames, and TXTAIL of flags (the last bits leaving
the audio path before the radio drops). Frames a digipeater repeats skip
the persistence draw, as direwolf's do: the channel was just cleared for
them.

**Busy** is the receiver's DCD (demod.Receiver.dcd: a bit clock locked to
something AFSK-shaped) or'd with whatever the radio link says about its own
squelch or busy state (`RadioLink` calls `busy_in`). Either alone is
enough to hold off: a radio's squelch opens on a voice user that our DCD
would never call data, and our DCD sees a weak packet station below a
radio's squelch. **Busy is waited on for MAX_BUSY_WAIT_S at most**: busy
without a break for that long is taken as an indication stuck on, not a
station, and the channel goes on as if clear (the persistence draw and
slot time still apply) until busy next drops - except a stretch in which
a station we hold a link with was heard sending us frames
(`partner_heard`), which is its over and is waited out
(PARTNER_MAX_WAIT_S). Only that wait is cut short:
the transmit permission and everything the link checks at keying are
untouched.

**The radio link** is abstract (`RadioLink`). An audio link (a sound card,
an IC-9700's USB codec, an IC-705 over its LAN stream) is keyed, sent PCM
and unkeyed; a frame link (a KISS TNC) takes the frames themselves. Either
feeds received audio or frames back into the engine and may report busy.
`turnaround_s` is the link's measured time from key() to the radio being
up to power: TXDELAY is never allowed below it plus MIN_PREAMBLE_S, because
flags sent into a radio that is not yet transmitting are not flags anyone
hears. The links themselves (IC-9700, IC-705, FTX-1, sound card) are in
`radios/`; sim.py has the simulated one the tests use.

**A link may refuse to key, or fail to**, at the moment of keying: each real
link checks the station again itself, and its radio can be gone. Either is
handled as a refused transmission (the frames are dropped and counted
against the link's reason key, the channel holds off, the app hears of it),
except that frames the data links handed over are told they were sent: a
frame that never reached the air is, to the link layer, a frame lost on
it, and its T1 retries it as it would any other.

**Transmit permission** is checked here, at the moment of keying, every
time (identity.Station.refusal): with no callsign or with transmit not
enabled the queued frames are dropped and counted, nothing is keyed, and
the link layer above finds out the way it would on a dead channel - its
timers expire.

**Deciding and keying are two steps** (`decide`, `act`, `finish`; `poll`
does all three for a caller with one thread). Keying a real radio waits on
the radio's own thread - an Icom's asyncio loop, a CI-V round trip - and the
engine decides under its lock, which that radio's received audio and busy
also need. Held across the key, the two waited on each other until the
radio call timed out (the IC-9700, 18/10/2026: radios/base.py). So the
engine decides under its lock, keys, unkeys and modulates outside it, and
takes the lock again only to record what happened.

Time is whatever the engine's clock says (`now`, seconds): the simulated
channel runs on simulated time, the app on time.monotonic().
"""

from __future__ import annotations

import abc
import collections
import logging
import random
import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import afsk, modem
from .errors import PacketError
from .identity import Station

log = logging.getLogger(__name__)

#: The shortest preamble a receiver can lock on, kept after the radio's
#: own turnaround: about a dozen flags at 1200 baud. At other speeds it is
#: the mode's own count of bits (modem.Mode.lock_bits), not this time:
#: a receiver locks in bits, and 80 ms is 12 288 of them at 153 600 baud.
MIN_PREAMBLE_S = 0.08

#: How long to wait after a refused transmission before trying again, so
#: that a disarmed station with a link layer full of retries does not spin.
REFUSED_BACKOFF_S = 1.0


#: The longest ChannelAccess waits on busy without a break before it
#: treats the channel as clear (module docstring), logging a warning once.
#: Longer than any one over a packet station makes - this engine's is capped
#: at ChannelConfig.max_tx_s, 10 s, and a TNC's is a few frames - with a
#: few slots to spare, so a real station is never talked over; short
#: enough that a connection's T1 and its N2 retries are not all spent
#: waiting. On 19/10/2026 (the first on-air run with GM0OTB) a busy held
#: for about a minute on an empty channel kept everything off the air; the
#: cause found is fixed (engine.Engine.mode_in), and this is for the next
#: one nobody has found yet. A voice user on a packet channel for longer
#: than this is talked over: the price of never being held off for good.
MAX_BUSY_WAIT_S = 15.0

#: Busy that began before a frame from our own link partner was decoded,
#: and has not dropped since, is that station's over (or its window of
#: overs back to back) and is waited out to its end rather than cut short
#: at MAX_BUSY_WAIT_S (`partner_heard`) - for PARTNER_MAX_WAIT_S at most,
#: so a squelch stuck open just after a partner's over still costs only a
#: bounded wait. Other stations, and a carrier nobody decodes, keep the cap.
#:
#: Why (Packet Radio 0.3.1's transfer estimates, 20/10/2026): with k 63 and
#: PACLEN 512 at 9600 a sender's window is two 10 s overs with a tenth of a
#: second between; the receiver, holding an acknowledgement, read busy for
#: over 15 s, took it for a stuck squelch and transmitted into the second
#: over, losing half of every window and waiting out a T1 after it. A
#: station sending us frames is not a stuck squelch.
#:
#: Not "while its frames keep coming": receivers hand frames over in
#: blocks (a Pluto's receive buffers, a KISS TNC, the simulator's frames
#: medium at the end of each over), so a gap in decoded frames says little
#: about whether the partner is still on the air. 45 s covers a window of
#: 63 frames of 512 bytes at 9600 (about 28 s of overs) with room.
PARTNER_MAX_WAIT_S = 45.0


#: How long a frame we sent is remembered for `ChannelAccess.own_echo`: longer
#: than any receive pipeline's lag, short enough that the list stays small.
OWN_ECHO_S = 30.0


@dataclass
class ChannelConfig:
    """The KISS parameters, in their usual units. Defaults are the
    conventional 1200-baud ones (the KISS specification's and direwolf's):
    TXDELAY 300 ms, TXTAIL 30 ms (direwolf's 10 ms is too short for some
    USB sound cards), SLOTTIME 100 ms, PERSIST 63 (p = 0.25)."""
    txdelay_s: float = 0.30
    txtail_s: float = 0.03
    slottime_s: float = 0.10
    persist: int = 63
    #: A full-duplex channel (a satellite, a crossband repeater) ignores busy.
    full_duplex: bool = False
    #: None for plain AX.25; "auto", 16, 32 or 64 for FX.25 (afsk.modulate).
    fx25: object = None
    #: The most frames in one transmission and the longest one may be: a
    #: queue that has grown must not hold the channel for a minute.
    max_frames: int = 8
    max_tx_s: float = 10.0
    #: The modulator's level, of full scale.
    amplitude: float = 0.5
    #: The modem (modem.py): 1200 is AFSK; 9600, 19200 and 38400 are G3RUH
    #: FSK, which only a link that carries baseband can send (a Pluto with
    #: its own `baud`). None takes the link's (`RadioLink.baud`), and the
    #: engine refuses a channel and a link that disagree.
    baud: int | None = None
    #: The modem by name (modem.MODES: "gfsk76800", "4fsk38400"...), for
    #: the modes a number cannot name. Given, it sets `baud` to its bit rate.
    mode: str | None = None

    def __post_init__(self):
        if not 0 <= self.persist <= 255:
            raise PacketError(f"persist {self.persist} is not 0-255")
        if self.mode is not None:
            m = modem.mode(self.mode)
            if self.baud is not None and int(self.baud) != m.bitrate:
                raise PacketError(f"mode {m.name} is {m.bitrate} bit/s, not {self.baud}")
            self.mode, self.baud = m.name, m.bitrate
        elif self.baud is not None:
            self.baud = modem.check(self.baud)
            self.mode = modem.mode(self.baud).name


class LinkSink(abc.ABC):
    """What a radio link calls back into (the Engine)."""

    @abc.abstractmethod
    def pcm_in(self, pcm) -> None:
        """Received audio: s16le bytes or float samples at the link's rate."""

    @abc.abstractmethod
    def frame_in(self, frame: bytes) -> None:
        """A received frame without its FCS (frame links: a KISS TNC)."""

    @abc.abstractmethod
    def busy_in(self, busy: bool) -> None:
        """The radio's squelch or busy indication changed."""

    def level_in(self, level_db: float, noise_bw_hz: float) -> None:
        """The level of the audio about to be handed in (`pcm_in`), from a
        link that measures it: dB over the channel's floor through a filter
        of `noise_bw_hz`. What a frame decoded from that audio was heard
        at. Optional for a sink; most links never call it."""

    def link_event(self, kind: str, reason: str, detail: dict | None = None) -> None:
        """Something the radio link did on its own that the app should hear
        of: an unkey by its watchdog, a radio gone, a squelch stuck open.
        `reason` is a key from radios.REASONS. Optional for a sink."""


class RadioLink(abc.ABC):
    """A radio the engine can transmit on and hear from.

    The engine calls `attach` once, then `key`, `send_pcm` (or
    `send_frames`) and `unkey` for each transmission, unkeying when the
    audio it sent has had time to play. A link whose audio is buffered
    further down may hold the radio keyed until its buffer drains; that is
    its business. A link must never key on its own.
    """
    #: Sample rate of the audio both ways (audio links).
    rate: int = 12_000
    #: True for a link that carries frames rather than audio (KISS).
    carries_frames: bool = False
    #: Measured key-to-RF time, seconds; 0 if the link does not know.
    turnaround_s: float = 0.0
    #: The modem the link's audio is for (modem.py). An ordinary audio link
    #: is 1200 (AFSK through a voice radio); a link that carries baseband at
    #: a rate fast enough for G3RUH FSK says which baud it is set up for.
    baud: int = 1200
    #: Or which mode by name (modem.MODES), which wins over `baud`: the
    #: 4-FSK modes have no number of their own.
    mode: str | None = None

    def attach(self, sink: LinkSink) -> None:
        self.sink = sink

    @abc.abstractmethod
    def key(self) -> None: ...

    @abc.abstractmethod
    def unkey(self) -> None: ...

    def send_pcm(self, pcm: bytes) -> None:
        raise NotImplementedError

    def send_frames(self, frames: list[bytes], txdelay_s: float, airtime_s: float) -> None:
        raise NotImplementedError


#: A frame waiting to go, with a callback told when its transmission ends
#: (the link layer starts T1 from then, so the timer does not count the
#: frame's own time on air).
@dataclass
class Outgoing:
    frame: bytes
    on_sent: Callable[[float], None] | None = None
    #: The mode to send it at, on a link that adapts (adapt.py): an I frame
    #: at its connection's data mode; None, everything else, at the floor.
    mode: str | None = None


def airtime_s(frames: list[bytes], txdelay_s: float, txtail_s: float, fx25=None, baud: float = afsk.BAUD) -> float:
    """How long a transmission of these frames takes, preamble and tail
    included."""
    bits = afsk.transmission_bits(frames, fx25_mode=fx25, txdelay_ms=txdelay_s * 1000.0,
                                  tail_ms=txtail_s * 1000.0, baud=baud)
    return len(bits) / baud


def frame_airtime_s(frame: bytes, baud: float = afsk.BAUD) -> float:
    """One frame's own airtime: its bits after stuffing, and the flag after."""
    from . import hdlc
    return (len(hdlc.stuff(hdlc.with_fcs(frame))) + 8) / baud


@dataclass
class Plan:
    """What `decide` chose to do to the radio, for `act` to do outside the
    engine's lock and `finish` to record under it."""
    now: float
    #: When to be called next if this plan does not transmit.
    next: float | None = None
    #: The last transmission has played: unkey first.
    unkey: bool = False
    #: Frames to key up and send, with the TXDELAY they go out with.
    outgoing: list[Outgoing] = field(default_factory=list)
    txdelay_s: float = 0.0
    #: Filled in by `act`: the transmission's length, or why it failed.
    duration_s: float = 0.0
    error: Exception | None = None


@dataclass
class ChannelStats:
    transmissions: int = 0
    frames: int = 0
    airtime_s: float = 0.0
    refused: collections.Counter = field(default_factory=collections.Counter)
    refused_frames: int = 0
    #: Times busy was waited on for MAX_BUSY_WAIT_S and then gone through.
    busy_overrides: int = 0


class ChannelAccess:
    """p-persistent CSMA over one radio link.

    `queue()` takes frames to send; `providers` are asked for more at the
    moment of transmitting (the data links, so an acknowledgement carries
    the newest N(R) and I frames go when the window allows), each a callable
    `(now, room, budget_s) -> list[Outgoing]` giving at most `room` frames
    and `budget_s` seconds of airtime.
    """

    def __init__(self, station: Station, link: RadioLink | None, config: ChannelConfig | None = None,
                 rng: random.Random | None = None):
        self.station = station
        self.link = link
        self.config = config or ChannelConfig()
        self.rng = rng or random.Random()
        self.providers: list[Callable[[float, int, float], list[Outgoing]]] = []
        #: Something that wants to send if asked, without building it (the
        #: providers' "anything pending?"), so an idle channel costs nothing.
        self.wanting: list[Callable[[], bool]] = []
        self._urgent: collections.deque[Outgoing] = collections.deque()
        self._normal: collections.deque[Outgoing] = collections.deque()
        self.dcd = False
        self.radio_busy = False
        self.transmitting = False
        self._tx_end = 0.0
        self._next_slot: float | None = None
        self._hold_until = 0.0
        #: When DCD or the radio's busy came on and has stayed on since
        #: (None while neither is), and whether that stretch has outlasted
        #: MAX_BUSY_WAIT_S and is no longer waited on.
        self._busy_since: float | None = None
        self._busy_ignored = False
        #: When a frame from a station we have a link with was last decoded
        #: (`partner_heard`).
        self._partner_at: float | None = None
        self.stats = ChannelStats()
        #: Told each refusal: (key, frames dropped). The engine turns it into
        #: an event.
        self.on_refused: Callable[[str, int], None] | None = None
        #: Told each transmission's end time, for anything that wants to know
        #: when the channel is ours (tests, the app's TX indicator).
        self.on_transmit: Callable[[float, float, list[bytes]], None] | None = None
        #: Every frame this channel has put on the air in the last
        #: OWN_ECHO_S, as (when, bytes): what `own_echo` recognises when a
        #: full-duplex radio hands our own transmission back (see there).
        self._own: collections.deque[tuple[float, bytes]] = collections.deque()
        self._own_lock = threading.Lock()

    # -- input -----------------------------------------------------------------

    def queue(self, frame: bytes, *, urgent: bool = False, on_sent=None, mode: str | None = None) -> None:
        (self._urgent if urgent else self._normal).append(Outgoing(bytes(frame), on_sent, mode))

    def partner_heard(self, now: float) -> None:
        """A frame from a station we hold a connected link with was just
        decoded (the engine says so): the busy stretch it came in is that
        station's and is waited out (PARTNER_MAX_WAIT_S)."""
        if self._busy_since is None and (self.dcd or self.radio_busy) and not self.config.full_duplex:
            # Heard before the channel was next polled: the stretch began
            # no later than this.
            self._busy_since = now
        self._partner_at = now

    def discard(self, match: Callable[[bytes], bool]) -> int:
        """Take queued frames that `match` off both queues, unsent; how many.
        For frames to a station whose link has gone (returnpath.py)."""
        n = 0
        for q in (self._urgent, self._normal):
            keep = [o for o in q if not match(o.frame)]
            n += len(q) - len(keep)
            q.clear()
            q.extend(keep)
        return n

    @property
    def busy(self) -> bool:
        if self.config.full_duplex:
            return False
        return self.transmitting or self.dcd or self.radio_busy

    @property
    def mode(self) -> modem.Mode:
        """The channel's modem: the config's, else the link's, else AFSK 1200."""
        if self.config.mode is not None:
            return modem.mode(self.config.mode)
        return modem.of_link(self.link)

    @property
    def baud(self) -> int:
        """Bits a second on the air (the mode's bit rate: twice its baud for
        4-FSK), which is what every airtime sum here divides by."""
        return self.mode.bitrate

    @property
    def adaptive(self) -> bool:
        """The link sends several modes (adapt.py): it names its ladder."""
        return bool(getattr(self.link, "modes", None))

    @property
    def control_mode(self) -> modem.Mode:
        """What a frame with no mode of its own goes at: the ladder's floor
        on an adaptive link, else the channel's one mode."""
        if self.adaptive:
            return modem.mode(self.link.modes[-1])
        return self.mode

    def frame_airtime(self, o: Outgoing) -> float:
        bitrate = modem.mode(o.mode).bitrate if (o.mode and self.adaptive) else self.control_mode.bitrate
        return frame_airtime_s(o.frame, bitrate)

    def segments(self, outgoing: list[Outgoing]) -> list[tuple[modem.Mode, list[bytes]]]:
        """One transmission's frames by the mode they go at: everything at
        the control mode first, then each data mode in the order first
        met (adapt.py: acknowledgements never wait behind data)."""
        if not self.adaptive:
            return [(self.mode, [o.frame for o in outgoing])]
        control = self.control_mode
        groups: dict[str, list[bytes]] = {control.name: []}
        for o in outgoing:
            groups.setdefault(o.mode or control.name, []).append(o.frame)
        return [(modem.mode(name), frames) for name, frames in groups.items() if frames]

    def plan_airtime(self, outgoing: list[Outgoing], txdelay_s: float) -> float:
        """A transmission's length: the first part after TXDELAY, each
        later part after its own mode's lock bits."""
        total = 0.0
        parts = self.segments(outgoing)
        for i, (m, frames) in enumerate(parts):
            pre = txdelay_s if i == 0 else m.lock_bits / m.bitrate
            tail = self.config.txtail_s if i == len(parts) - 1 else 0.0
            total += airtime_s(frames, pre, tail, self.config.fx25, m.bitrate)
        return total

    @property
    def txdelay_s(self) -> float:
        """TXDELAY as sent: the config's, never less than the link's
        turnaround (time: the radio coming up) plus the mode's lock bits
        (bits: the far receiver locking on). On an adaptive link, the
        control mode's: a transmission may start with it."""
        turnaround = self.link.turnaround_s if self.link is not None else 0.0
        m = self.control_mode
        return max(self.config.txdelay_s, turnaround + m.lock_bits / m.bitrate)

    def pending(self) -> bool:
        return bool(self._urgent or self._normal) or any(w() for w in self.wanting)

    # -- the access procedure -------------------------------------------------------

    def poll(self, now: float) -> float | None:
        """Do whatever is due at `now`; returns when to be called next (None:
        when something changes - a frame queued, busy changing). For a
        caller with nothing else to lock; the engine splits it (module
        docstring)."""
        nxt, plan = self.decide(now)
        if plan is None:
            return nxt
        self.act(plan)
        return self.finish(plan)

    def decide(self, now: float) -> tuple[float | None, Plan | None]:
        """The access procedure up to the radio: when to be called next, and
        what to do to the radio now, if anything. Frames chosen are taken off
        the queues here, so a decision is carried out once."""
        unkey = False
        if self.transmitting:
            if now + 1e-9 < self._tx_end:
                return self._tx_end, None
            self.transmitting = False
            unkey = self.link is not None
        nxt, plan = self._access(now)
        if unkey:
            plan = plan or Plan(now, next=nxt)
            plan.unkey = True
        return nxt, plan

    def _access(self, now: float) -> tuple[float | None, Plan | None]:
        carrier = (self.dcd or self.radio_busy) and not self.config.full_duplex
        if not carrier:
            self._busy_since, self._busy_ignored = None, False
        elif self._busy_since is None:
            self._busy_since = now
        if not self.pending():
            self._next_slot = None
            return None, None
        if now < self._hold_until:
            return self._hold_until, None
        if carrier:                                 # (`decide` has seen our own transmission out)
            give_up = self._busy_since + MAX_BUSY_WAIT_S
            if self._partner_at is not None and self._partner_at >= self._busy_since:
                # Our link partner's over: waited out (PARTNER_MAX_WAIT_S).
                give_up = self._busy_since + PARTNER_MAX_WAIT_S
            if now + 1e-9 < give_up:
                # Wait for clear; DCD and busy arrive with the audio, and the
                # engine polls again when they do, or at the give-up time.
                # A slot is drawn afresh after.
                self._next_slot = None
                return give_up, None
            if not self._busy_ignored:
                self._busy_ignored = True
                self.stats.busy_overrides += 1
                log.warning("the channel has read busy for %.0f s without a break (DCD %s, radio busy %s); "
                            "going on as if clear until it drops", now - self._busy_since, self.dcd,
                            self.radio_busy)
        if self._urgent:
            return self._transmit(now)
        if self._next_slot is not None and now + 1e-9 < self._next_slot:
            return self._next_slot, None
        if self.rng.randrange(256) <= self.config.persist:
            return self._transmit(now)
        self._next_slot = now + self.config.slottime_s
        return self._next_slot, None

    def _transmit(self, now: float) -> tuple[float | None, Plan | None]:
        self._next_slot = None
        refusal = "no-radio" if self.link is None else self.station.refusal()
        if refusal is not None:
            dropped = len(self._urgent) + len(self._normal)
            self._urgent.clear()
            self._normal.clear()
            self.stats.refused[refusal] += 1
            self.stats.refused_frames += dropped
            self._hold_until = now + REFUSED_BACKOFF_S
            log.info("transmission refused (%s), %d queued frames dropped", refusal, dropped)
            if self.on_refused is not None:
                self.on_refused(refusal, dropped)
            return self._hold_until, None
        cfg = self.config
        outgoing: list[Outgoing] = []
        while self._urgent and len(outgoing) < cfg.max_frames:
            outgoing.append(self._urgent.popleft())
        while self._normal and len(outgoing) < cfg.max_frames:
            outgoing.append(self._normal.popleft())
        txdelay = self.txdelay_s
        budget = cfg.max_tx_s - txdelay - cfg.txtail_s - sum(self.frame_airtime(o) for o in outgoing)
        for provider in self.providers:
            room = cfg.max_frames - len(outgoing)
            if room <= 0:
                break
            got = provider(now, room, budget if outgoing else float("inf"))
            budget -= sum(self.frame_airtime(o) for o in got)
            outgoing += got
        # The airtime limit, for queued frames (the providers kept to it
        # themselves): what does not fit goes back to the front of the queue
        # for the next transmission. The first frame always goes.
        while len(outgoing) > 1 and self.plan_airtime(outgoing, txdelay) > cfg.max_tx_s:
            self._normal.appendleft(outgoing.pop())
        if not outgoing:
            return None, None
        return None, Plan(now, outgoing=outgoing, txdelay_s=txdelay)

    def act(self, plan: Plan) -> None:
        """Do to the radio what `decide` chose: outside the engine's lock,
        because a real link's key and unkey wait on the radio (module
        docstring). Never raises: a failure is kept in the plan, for
        `finish`, and a transmission that fails is unkeyed here."""
        link = self.link
        if plan.unkey:
            try:
                link.unkey()
            except Exception:                       # noqa: BLE001 - the link's own watchdogs carry on
                log.exception("unkeying at the end of a transmission")
        if not plan.outgoing:
            return
        cfg = self.config
        frames = [o.frame for o in plan.outgoing]
        # Recorded before the key, so an echo can never be decoded first.
        with self._own_lock:
            for f in frames:
                self._own.append((plan.now, f))
        try:
            link.key()
            if self.adaptive:
                self._act_adaptive(plan, link)
            elif link.carries_frames:
                plan.duration_s = airtime_s(frames, plan.txdelay_s, cfg.txtail_s, cfg.fx25, self.baud)
                link.send_frames(frames, plan.txdelay_s, plan.duration_s)
            else:
                audio = modem.modulate(frames, link.rate, mode=self.mode, amplitude=cfg.amplitude,
                                       fx25_mode=cfg.fx25, txdelay_ms=plan.txdelay_s * 1000.0,
                                       tail_ms=cfg.txtail_s * 1000.0)
                plan.duration_s = len(audio) / link.rate
                link.send_pcm(afsk.pcm16(audio))
        except Exception as e:                      # noqa: BLE001 - a link's refusal or its failure
            plan.error = e
            if (getattr(e, "reason", None) or "radio-error") == "radio-error":
                log.exception("the radio link failed while keying")
            else:
                log.info("the radio link refused to key: %s", getattr(e, "reason", e))
            try:
                link.unkey()
            except Exception:                       # noqa: BLE001 - unkeying regardless
                log.exception("unkeying after a failed key")

    def _act_adaptive(self, plan: Plan, link) -> None:
        """One transmission in parts by mode (`segments`): for a frame link
        the frames and their modes; for a baseband link each part
        modulated at the link's one rate (a whole number of samples a
        symbol for every mode on its ladder), its level scaled to its own
        deviation against the link's, joined. The link's FM modulator
        carries the phase across the joins."""
        cfg = self.config
        parts = self.segments(plan.outgoing)
        plan.duration_s = self.plan_airtime(plan.outgoing, plan.txdelay_s)
        if link.carries_frames:
            frames = [f for _m, fs in parts for f in fs]
            modes = [m.name for m, fs in parts for _f in fs]
            link.send_frames(frames, plan.txdelay_s, plan.duration_s, modes=modes)
            return
        pieces = []
        for i, (m, frames) in enumerate(parts):
            pre = plan.txdelay_s if i == 0 else m.lock_bits / m.bitrate
            tail = cfg.txtail_s if i == len(parts) - 1 else 0.0
            level = cfg.amplitude * m.deviation_hz / link.deviation_hz
            pieces.append(modem.modulate(frames, link.rate, mode=m, amplitude=level, fx25_mode=cfg.fx25,
                                         txdelay_ms=pre * 1000.0, tail_ms=tail * 1000.0))
        audio = np.concatenate(pieces)
        plan.duration_s = len(audio) / link.rate
        link.send_pcm(afsk.pcm16(audio))

    def finish(self, plan: Plan) -> float | None:
        """Record what `act` did; returns when to be called next."""
        if not plan.outgoing:
            return plan.next
        now = plan.now
        if plan.error is not None:
            return self._link_failed(now, plan.error, plan.outgoing)
        frames = [o.frame for o in plan.outgoing]
        duration = plan.duration_s
        self.transmitting = True
        self._tx_end = now + duration
        self.stats.transmissions += 1
        self.stats.frames += len(frames)
        self.stats.airtime_s += duration
        for o in plan.outgoing:
            if o.on_sent is not None:
                o.on_sent(self._tx_end)
        if self.on_transmit is not None:
            self.on_transmit(now, self._tx_end, frames)
        return self._tx_end

    def own_echo(self, frame: bytes, now: float) -> bool:
        """Whether `frame` is one this channel sent in the last OWN_ECHO_S,
        byte for byte: our own transmission heard back.

        A rig never hears itself; an SDR used as the radio does - a Pluto
        receives all through its own overs (radios.pluto) - and so does an
        SDR monitor beside the radio. Acted on, our own frames are frames
        to and from our own callsign: on 18/10/2026 station GM4BRI-2 on a
        Pluto heard its own SABM to GM4BRI-1, took GM4BRI-1 as its own
        address (identity.Station.owns: any SSID), refused the "incoming"
        connection with a DM from GM4BRI-1 - transmitted, over the IC-9700's
        UA - and its connect never completed. A digipeated copy differs (the
        H bit is set), so APRS still hears its own frames via a digi."""
        cutoff = now - OWN_ECHO_S
        with self._own_lock:
            while self._own and self._own[0][0] < cutoff:
                self._own.popleft()
            return any(f == frame for _t, f in self._own)

    def _link_failed(self, now: float, error: Exception, outgoing: list[Outgoing]) -> float:
        """The link refused to key, or failed while keying (module docstring);
        `act` has logged it and unkeyed."""
        reason = getattr(error, "reason", None) or "radio-error"
        self.stats.refused[reason] += 1
        self.stats.refused_frames += len(outgoing)
        self._hold_until = now + REFUSED_BACKOFF_S
        for o in outgoing:
            if o.on_sent is not None:
                o.on_sent(now)
        if self.on_refused is not None:
            self.on_refused(reason, len(outgoing))
        return self._hold_until
