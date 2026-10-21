"""The packet engine: a station's AX.25 layer two, without a GUI and
without a radio.

    station = Station.of("GM4BRI", tx_enabled=True)
    engine = Engine(station, radio)              # radio: a channel.RadioLink
    conn = engine.connect("GB7BBS", via=["MB7UC"])
    conn.on_data = lambda data: print(data)
    conn.send(b"L\\r")
    ...
    while True:
        engine.poll()                               # or Runner(engine).start()

One engine is one radio channel. It owns the receiver (audio in, frames
out, DCD), channel access (channel.py), the connected-mode links
(datalink.py), and whatever services are attached to it: the digipeater,
the APRS station, the mailbox, IP. The desktop app runs one now; the
OpenHamShack controller can run the same thing for a BBS or a node later,
which is why there is nothing here but bytes in and bytes out.

**Time** comes from `clock` (time.monotonic by default; sim.SimClock in the
tests) and nothing here sleeps: `poll()` does what is due and says when it
wants to be called again. `Runner` is the small thread that does that
against the wall clock, for an app and for the live direwolf test.

**Who may connect.** `listen(address, accept)`: an incoming SABM to one of
the station's addresses goes to each listener for that address in turn;
`accept(IncomingCall)` returns a callable that is given the new
Connection, or None to pass. If no listener takes it the caller gets DM -
including when nothing listens at all, which is the default: an engine
accepts no connections until the app (or the mailbox, when the operator
has allowed it) says so.

**The lock is never held while waiting on another thread.** `lock`
guards the engine's state, and the radio's threads take it to hand in
audio, busy and events. So the engine decides under it and acts outside
it: `poll` keys, unkeys and modulates between two holds of the lock
(channel.py), and `pcm_in` demodulates outside it, taking it only to act on
what was heard. Held across a key, it deadlocked an IC-9700 against the
engine until the radio call timed out (radios/base.py, 18/10/2026).

**Frames in transit are not ours.** A frame with a digipeater that has not
yet repeated it is on its way through that digipeater, and is only acted on
(other than by our own digipeater) once every hop has repeated it, as
AX.25 says. Otherwise every station would answer a SABM twice: once
direct and once through the digipeater.

**A return path** (returnpath.py), attached for one remote station, carries
that station's side of a link test that has only one RF direction: an RF
receiver's frames to it go there instead of to the channel, and frames
from it arrive there instead of on the air. Off unless the app attaches
one; every frame through it is counted, tapped and marked "internet".

**Taps** (`taps`) see what the station hears and sends, for telemetry and
logs: `tap(kind, detail)` with kind "heard" (frame, raw, via, mode, cn0,
decode_lag_s), "transmitted" (start, end, frames), "return-path"
(direction, frame, raw) and "event" (event). A tap that raises is logged
and skipped; a tap must not block, because it is called under the lock.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import ax25, modem
from .ax25 import PID_NONE, PID_SEGMENT, Address, Control, Frame, FrameType
from .channel import ChannelAccess, ChannelConfig, LinkSink, Outgoing, RadioLink, frame_airtime_s
from .datalink import DataLink, LinkConfig, State
from .errors import PacketError
from .identity import Station
from .returnpath import RETURN_SETTLE_S

log = logging.getLogger(__name__)


def _addr(value) -> Address:
    if isinstance(value, Address):
        return Address(value.call, value.ssid)
    return Address.from_text(str(value), flag=False)


def in_transit(frame: Frame) -> bool:
    """True while a digipeater named in the path has yet to repeat it."""
    return any(not hop.repeated for hop in frame.via)


def _key(address: Address) -> str:
    return f"{address.call}-{address.ssid}"


@dataclass
class Event:
    """Something the app may want to show. `kind` is what happened and
    `reason` a key from datalink.REASONS, identity.REFUSALS or a service's
    own table; `connection` is set for link events."""
    kind: str
    reason: str = ""
    connection: Connection | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class IncomingCall:
    """An incoming SABM, for a listener to accept or pass."""
    local: Address
    remote: Address
    via: list[Address]
    modulo: int


class Connection:
    """A connected-mode session as a byte stream.

    `send(data)` cuts it into I frames of at most N1 bytes; received data
    goes to `on_data(bytes)` if it is set, else into a buffer `read()`
    drains - and while more than LinkConfig.rx_limit bytes sit unread the
    link says RNR, so a peer cannot run this station out of memory and a
    slow reader slows the sender instead of losing data. `on_event(Event)`
    hears connected, disconnected, reset and busy. Data with a PID other
    than "no layer 3" (IP, in virtual-circuit mode) goes to
    `on_packet(pid, data)` instead and never into the stream.
    """

    def __init__(self, engine: Engine, link: DataLink):
        self.engine = engine
        self.link = link
        self.on_data: Callable[[bytes], None] | None = None
        self.on_event: Callable[[Event], None] | None = None
        self.on_packet: Callable[[int, bytes], None] | None = None
        self._rx = bytearray()
        self._segments: tuple[int, bytearray, int] | None = None
        self.sent_bytes = 0
        self.received_bytes = 0
        self.reason = ""
        link.deliver = self._deliver
        link.events = self._event

    def __repr__(self):
        return f"<Connection {self.local}>{self.remote} {self.state.value}>"

    @property
    def local(self) -> Address:
        return self.link.local

    @property
    def remote(self) -> Address:
        return self.link.remote

    @property
    def via(self) -> list[Address]:
        return self.link.via

    @property
    def state(self) -> State:
        return self.link.state

    @property
    def connected(self) -> bool:
        return self.link.connected

    @property
    def modulo(self) -> int:
        return self.link.modulo

    @property
    def return_path(self):
        """The return path this connection's station has (returnpath.py), or None."""
        return self.engine.return_paths.get(_key(self.remote))

    @property
    def pending(self) -> int:
        """I frames sent but not acknowledged, or not yet sent."""
        return self.link.queued

    def send(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("latin-1")
        with self.engine.lock:
            n1 = self.link.n1
            for at in range(0, len(data), n1):
                self.link.data(data[at:at + n1])
            self.sent_bytes += len(data)
        self.engine._wake()

    def send_packet(self, pid: int, packet: bytes) -> bool:
        """One layer-3 packet with this PID (IP in virtual-circuit mode),
        kept whole: in one I frame if it fits N1, else cut by the AX.25 2.2
        segmenter (§6.6, PID 0x08), as direwolf does. False if even that
        cannot carry it (more than 128 segments)."""
        with self.engine.lock:
            return self._send_packet(pid, packet)

    def _send_packet(self, pid: int, packet: bytes) -> bool:
        n1 = self.link.n1
        if len(packet) <= n1:
            self.link.data(packet, pid)
        else:
            # The first segment carries the original PID after its header
            # byte; each header byte says how many segments follow.
            body = bytes([pid]) + packet
            room = n1 - 1
            pieces = [body[at:at + room] for at in range(0, len(body), room)]
            if len(pieces) > 128:
                return False
            for i, piece in enumerate(pieces):
                header = (0x80 if i == 0 else 0) | (len(pieces) - 1 - i)
                self.link.data(bytes([header]) + piece, PID_SEGMENT)
        self.engine._wake()
        return True

    def read(self, n: int = -1) -> bytes:
        with self.engine.lock:
            return self._read(n)

    def _read(self, n: int) -> bytes:
        if n < 0 or n >= len(self._rx):
            out, self._rx = bytes(self._rx), bytearray()
        else:
            out, self._rx = bytes(self._rx[:n]), self._rx[n:]
        if self.link.own_busy and len(self._rx) <= self.link.config.rx_limit // 2:
            self.link.set_busy(False)
            self.engine._wake()
        return out

    def set_busy(self, busy: bool) -> None:
        """Tell the peer to stop (RNR) or carry on, whatever the buffer."""
        with self.engine.lock:
            self.link.set_busy(busy)
        self.engine._wake()

    def close(self) -> None:
        """Disconnect once what was sent has been acknowledged."""
        with self.engine.lock:
            self.link.close()
        self.engine._wake()

    def abort(self) -> None:
        """Disconnect now, dropping anything unsent (DL-DISCONNECT)."""
        with self.engine.lock:
            self.link.disconnect()
        self.engine._wake()

    def _reassemble(self, data: bytes) -> tuple[int, bytes] | None:
        """One segment in; the whole packet out when its last arrives.

        direwolf segments anything an application hands it that is longer
        than its PACLEN - plain text included - so this is needed to hear a
        direwolf station at all. A segment out of order (a first while a
        packet is open, or a count that does not follow) drops what was
        collected, as the specification's reassembler does."""
        if not data:
            return None
        header, rest = data[0], data[1:]
        remaining = header & 0x7F
        if header & 0x80:
            if not rest:
                self._segments = None
                return None
            self._segments = (rest[0], bytearray(rest[1:]), remaining)
        elif self._segments is None or remaining != self._segments[2] - 1:
            self._segments = None
            self.engine.stats["segment_errors"] += 1
            return None
        else:
            pid, collected, _ = self._segments
            collected += rest
            self._segments = (pid, collected, remaining)
        if remaining == 0:
            pid, collected, _ = self._segments
            self._segments = None
            return pid, bytes(collected)
        return None

    def _deliver(self, pid: int, data: bytes) -> None:
        if pid == PID_SEGMENT:
            whole = self._reassemble(data)
            if whole is None:
                return
            pid, data = whole
        if pid != PID_NONE:
            if self.on_packet is not None:
                self.on_packet(pid, data)
            else:
                self.engine._packet_from_link(self, pid, data)
            return
        self.received_bytes += len(data)
        if self.on_data is not None:
            self.on_data(data)
            return
        self._rx += data
        if len(self._rx) > self.link.config.rx_limit and not self.link.own_busy:
            self.link.set_busy(True)

    def _event(self, kind: str, reason: str) -> None:
        self.reason = reason
        event = Event(kind, reason, self)
        if self.on_event is not None:
            self.on_event(event)
        self.engine._emit(event)
        if kind == "disconnected":
            self.engine._forget(self)


class Engine(LinkSink):
    def __init__(self, station: Station, radio: RadioLink | None = None, *,
                 channel: ChannelConfig | None = None, link: LinkConfig | None = None,
                 clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None,
                 block_ms: float = 20.0, diversity: bool = True):
        self.station = station
        self.radio = radio
        self.clock = clock
        self.rng = rng or random.Random()
        self.link_config = link or LinkConfig()
        if getattr(radio, "modes", None) and self.link_config.adapt is None:
            # An adaptive link: the connections adapt over its ladder.
            self.link_config = dataclasses.replace(self.link_config, adapt=tuple(radio.modes))
        self.channel = ChannelAccess(station, radio, channel, self.rng)
        self.channel.providers.append(self._link_frames)
        self.channel.wanting.append(self._links_want)
        self.channel.on_refused = self._refused
        self.channel.on_transmit = self._transmitted
        # 20 ms blocks rather than the receiver's 100 ms default: DCD is
        # read once a block, and CSMA cannot hold off for longer than it
        # takes to notice the channel is busy. Measured cost at 12 kHz: the
        # same per second of audio within a few per cent.
        self.receiver = None
        if radio is not None and not radio.carries_frames:
            # The channel's modem and the link's must agree: a 9600 baud
            # engine on a link that only carries voice audio would key up
            # and send baseband nobody can decode (modem.py).
            want, carries = self.channel.mode, modem.of_link(radio)
            if want != carries:
                raise PacketError(f"the channel is set for {want.name} but the radio link carries {carries.name}")
            if not getattr(radio, "modes", None):
                self.receiver = modem.receiver(radio.rate, mode=want, block_ms=block_ms, diversity=diversity)
        #: An adaptive link's receivers, one per mode it hands us (mode_in),
        #: each made when its mode first arrives; and the modes we ask the
        #: link to listen to (adapt.py).
        self.receivers: dict[str, object] = {}
        self._receiver_start: dict[str, float] = {}
        self._stream_s = 0.0
        #: The level of the audio pcm_in is about to be handed (level_in),
        #: from a link that measures it; used once, by that audio.
        self._rx_level: tuple[float, float] | None = None
        self._block_ms = block_ms
        self._listening: frozenset[str] | None = None
        #: Modes to demodulate whatever the connections want: a path test
        #: listens on every mode on the ladder (pathtest.py).
        self.listen_extra: set[str] = set()
        self.last_heard: tuple[str | None, float | None] = (None, None)
        self.connections: dict[tuple[str, str], Connection] = {}
        self.listeners: list[tuple[str, Callable[[IncomingCall], object]]] = []
        #: Every UI frame heard (after the digipeater): (frame) -> None.
        self.ui_handlers: list[Callable[[Frame], None]] = []
        #: Every frame heard, as heard: (frame) -> None. The app's monitor.
        self.monitors: list[Callable[[Frame], None]] = []
        #: Services with timers of their own: objects with poll(now) and
        #: next_deadline() (the APRS station, IP's ARP, the digipeater).
        self.services: list = []
        #: Frames with a PID the stream does not take, from a connection
        #: with no on_packet: (connection, pid, data). IP sets this.
        self.packet_handlers: dict[int, Callable[[Connection, int, bytes], None]] = {}
        self.digipeater = None
        self.on_event: Callable[[Event], None] | None = None
        #: Observers (module docstring): (kind, detail) -> None.
        self.taps: list[Callable[[str, dict], None]] = []
        #: Return paths by remote station (returnpath.py); none by default.
        self.return_paths: dict[str, object] = {}
        self._rp_clear_since: float | None = None
        self.events: collections.deque[Event] = collections.deque(maxlen=1000)
        self.stats = collections.Counter()
        self._wake_event = threading.Event()
        self.lock = threading.RLock()
        #: One poll at a time: a decision is carried out before the next is
        #: made, though the lock is let go in between (module docstring).
        self._polling = threading.RLock()
        #: One demodulator feed at a time, outside `lock`.
        self._feeding = threading.Lock()
        if radio is not None:
            radio.attach(self)
            self._update_listening()

    # -- the radio side (LinkSink) --------------------------------------------------

    def level_in(self, level_db: float, noise_bw_hz: float) -> None:
        self._rx_level = (level_db, noise_bw_hz)

    def pcm_in(self, pcm) -> None:
        if self.receiver is None:
            return
        from .adapt import cn0_from_level
        # The demodulator is the costly part and touches nothing but the
        # receiver, so it runs outside the lock (module docstring).
        with self._feeding:
            heard = list(self.receiver.feed(pcm))
            dcd = self.receiver.dcd
            # The C/N0 a frame from this audio was heard at, where the link
            # measured it (an SDR's receiver; a rig's audio has no level):
            # what mode_in gives an adaptive link's frames, for one mode.
            level, self._rx_level = self._rx_level, None
            cn0 = cn0_from_level(*level) if level is not None else None
        with self.lock:
            for h in heard:
                self._frame_in(h.frame, None, cn0)
            self.channel.dcd = dcd
        self._wake()

    def frame_in(self, frame: bytes, mode: str | None = None, cn0: float | None = None) -> None:
        with self.lock:
            self._frame_in(frame, mode, cn0)
            self._update_listening()
        self._wake()

    def mode_in(self, bundle) -> None:
        """An adaptive link's received audio: one block of IQ as each mode's
        discriminator output (radios.sdrrx.ModeBundle). Each mode has its
        own receiver; the frames all of them found are taken in the order
        they were on the air, because two receivers' decodes of one block
        come out in the order they are run, and an older N(R) taken after
        a newer one resets a link."""
        from .adapt import cn0_from_level
        heard = []
        # DCD is any receiver's in this bundle, never one remembered from an
        # earlier bundle: a mode the link stops sending (no longer listened
        # to, adapt.py and the path test change that set all the time) has
        # no receiver running to clear it. Kept across bundles, a DCD that
        # was up as its mode was dropped - a frame's tail still in it - held
        # the channel busy, and every transmission off, until that mode was
        # next listened to. That is the one way found to hold busy for a
        # minute on an empty channel, as the first on-air run with GM0OTB
        # did (19/10/2026); the SDR's level busy cannot (radios.sdrrx,
        # FLOOR_S).
        dcd = False
        with self._feeding:
            for part in bundle.parts:
                rx = self.receivers.get(part.mode)
                if rx is None:
                    rx = self.receivers[part.mode] = modem.receiver(part.rate, mode=part.mode,
                                                                    block_ms=self._block_ms)
                    self._receiver_start[part.mode] = self._stream_s
                start = self._receiver_start[part.mode]
                cn0 = cn0_from_level(part.level_db, part.noise_bw_hz)
                for h in rx.feed(part.samples):
                    heard.append((start + h.sample / part.rate, h.frame, part.mode, cn0))
                dcd = dcd or bool(rx.dcd)
            self._stream_s += bundle.seconds
            stream_end = self._stream_s
        heard.sort(key=lambda x: x[0])
        with self.lock:
            for t, frame, mode, cn0 in heard:
                # How far behind the end of the audio handed in the frame
                # was found (taps): the receiver's own delay, not the radio's.
                self._frame_in(frame, mode, cn0, lag=stream_end - t)
                self.stats["heard_" + mode] += 1
            self.channel.dcd = dcd
            self._update_listening()
        self._wake()

    def listen_also(self, modes) -> None:
        """Demodulate these modes as well, from now (an adaptive link; a
        path test hears every mode on the ladder)."""
        with self.lock:
            self.listen_extra = set(modes)
            self._update_listening()

    def _update_listening(self) -> None:
        """Tell an adaptive link which modes to demodulate: the floor and
        the top always, and what each connection's adapter wants."""
        radio = self.radio
        modes = getattr(radio, "modes", None)
        if not modes or not hasattr(radio, "listen_modes"):
            return
        want = {modes[0], modes[-1]} | (set(self.listen_extra) & set(modes))
        now = self.clock()
        for conn in self.connections.values():
            if conn.link.adapter is not None:
                want |= conn.link.adapter.listening(now)
        want = frozenset(want)
        if want != self._listening:
            self._listening = want
            radio.listen_modes(set(want))

    def busy_in(self, busy: bool) -> None:
        """The radio's squelch or busy state, or any other carrier detect a
        link can offer (a KISS TNC's, an SDR's): or'd with our DCD."""
        with self.lock:
            self.channel.radio_busy = bool(busy)
        self._wake()

    def link_event(self, kind: str, reason: str, detail: dict | None = None) -> None:
        """An event from the radio link (radios.REASONS), as an engine Event."""
        self._emit(Event(kind, reason, detail=dict(detail or {})))

    def set_dcd(self, dcd: bool) -> None:
        """The DCD hook for a link that has its own demodulator."""
        with self.lock:
            self.channel.dcd = bool(dcd)
        self._wake()

    # -- sending ------------------------------------------------------------------

    def send_frame(self, frame: Frame, *, on_sent=None, mode: str | None = None) -> None:
        """Queue a frame of our own (the services' way out). `mode`: on an
        adaptive link, the mode to send it at (a path test's frames);
        None is the ladder's floor."""
        if not self.station.owns(frame.source):
            raise PacketError(f"{frame.source} is not this station's callsign")
        with self.lock:
            self.channel.queue(frame.encode(), on_sent=on_sent, mode=mode)
        self._wake()

    def repeat(self, frame: Frame) -> None:
        """Queue a frame the digipeater is repeating: someone else's, and
        sent without the persistence draw (channel.py)."""
        with self.lock:
            self.channel.queue(frame.encode(), urgent=True)
        self._wake()

    def send_ui(self, destination, info: bytes | str, *, via=(), pid: int = PID_NONE,
                source: Address | None = None, mode: str | None = None) -> Frame:
        frame = Frame.ui(source or self.station.address(), _addr(destination), info,
                         via=[_addr(v) for v in via], pid=pid)
        self.send_frame(frame, mode=mode)
        return frame

    # -- connections --------------------------------------------------------------

    def connect(self, remote, *, via=(), local: Address | None = None, modulo: int | None = None,
                config: LinkConfig | None = None) -> Connection:
        """DL-CONNECT to `remote` through `via` (digipeaters, in order).
        Refused while `remote` has a return path that is down."""
        local = _addr(local) if local is not None else self.station.address()
        remote = _addr(remote)
        rp = self.return_paths.get(_key(remote))
        if rp is not None and not rp.up:
            raise PacketError(f"the return path to {remote} is down")
        cfg = config or self.link_config
        if modulo is not None and modulo != cfg.modulo:
            cfg = LinkConfig(**{**cfg.__dict__, "modulo": modulo})
        with self.lock:
            key = (_key(local), _key(remote))
            old = self.connections.get(key)
            if old is not None and old.state is not State.DISCONNECTED:
                return old
            conn = self._new_connection(local, remote, [_addr(v) for v in via], cfg)
            conn.link.connect()
        self._wake()
        return conn

    def listen(self, address, accept: Callable[[IncomingCall], object]) -> None:
        """Offer incoming connections to `address` (one of the station's)
        to `accept`; see the module docstring."""
        self.listeners.append((_key(_addr(address)), accept))

    def unlisten(self, accept) -> None:
        self.listeners = [(a, f) for a, f in self.listeners if f is not accept]

    def connection(self, local, remote) -> Connection | None:
        return self.connections.get((_key(_addr(local)), _key(_addr(remote))))

    def _new_connection(self, local, remote, via, cfg) -> Connection:
        link = DataLink(local, remote, via, cfg, send=functools.partial(self._send_to, _key(remote)),
                        now=self.clock)
        link.baud = self.channel.baud               # its airtime budget counts at the channel's speed
        conn = Connection(self, link)
        self.connections[(_key(local), _key(remote))] = conn
        return conn

    def _forget(self, conn: Connection) -> None:
        key = (_key(conn.local), _key(conn.remote))
        if self.connections.get(key) is conn:
            del self.connections[key]

    def _link_send(self, out: Outgoing) -> None:
        self.channel.queue(out.frame, on_sent=out.on_sent, mode=out.mode)

    def _send_to(self, remote: str, out: Outgoing) -> None:
        """A link's frame to `remote`: by its outbound return path if it
        has one, else to the channel."""
        rp = self._outbound(remote)
        if rp is None:
            self._link_send(out)
        else:
            self._via_return(rp, out)

    def _link_frames(self, now: float, room: int, budget_s: float = float("inf")) -> list[Outgoing]:
        out: list[Outgoing] = []
        for conn in list(self.connections.values()):
            if len(out) >= room:
                break
            if self._outbound(_key(conn.remote)) is not None:
                continue                            # its frames go by return path (poll)
            got = conn.link.frames_for_tx(now, room - len(out), budget_s if not out else
                                          budget_s - sum(self.channel.frame_airtime(o) for o in out))
            out += got
        return out

    def _links_want(self) -> bool:
        return any(c.link.wants_to_send() for c in self.connections.values()
                   if self._outbound(_key(c.remote)) is None)

    # -- return paths (returnpath.py) -------------------------------------------------

    def attach_return_path(self, path) -> None:
        """Carry `path.remote`'s side of a link test by `path` (module
        docstring). One per remote station."""
        key = _key(path.remote)
        with self.lock:
            old = self.return_paths.get(key)
            if old is not None and old is not path:
                raise PacketError(f"{path.remote} already has a return path")
            self.return_paths[key] = path
            path.bind(self)
            if hasattr(path, "poll") and path not in self.services:
                self.services.append(path)
        log.warning("return path (%s) attached for %s: %s", path.name, key,
                    "every frame to it goes by return path, not by radio" if path.outbound
                    else "its frames are taken from the return path")
        self._emit(Event("return-path", "on", detail=path.describe()))
        self._wake()

    def detach_return_path(self, remote) -> None:
        """Take a station's return path away; links to it are dropped
        without transmitting, as when it is lost."""
        key = _key(_addr(remote))
        with self.lock:
            path = self.return_paths.get(key)
            if path is None:
                return
            self._drop_for(key, "return-path-off")
            del self.return_paths[key]
            if path in self.services:
                self.services.remove(path)
        log.warning("return path (%s) for %s detached", path.name, key)
        self._emit(Event("return-path", "off", detail=path.describe()))
        self._wake()

    def return_path(self, remote) -> object | None:
        return self.return_paths.get(_key(_addr(remote)))

    def _outbound(self, remote: str):
        rp = self.return_paths.get(remote)
        return rp if rp is not None and rp.outbound else None

    def _via_return(self, rp, out: Outgoing) -> None:
        """One frame on a return path, under the lock: it leaves now, so
        T1 runs from now (on_sent)."""
        now = self.clock()
        if not rp.up:
            self.stats["rp_dropped"] += 1
            log.info("return path (%s) down: frame to %s dropped", rp.name, rp.remote)
        else:
            rp.send(out.frame)
            self.stats["rp_sent"] += 1
            frame = ax25.parse(out.frame)
            log.info("by %s, not radio: %s", rp.name, frame.monitor() if frame is not None else out.frame.hex())
            self._tap("return-path", direction="out", frame=frame, raw=out.frame, via=rp.name, t=now)
        if out.on_sent is not None:
            out.on_sent(now)

    def _return_answers(self, conn: Connection, rp, now: float) -> float | None:
        """An RF receiver's answers to `conn`'s remote, once the channel
        has been clear for RETURN_SETTLE_S (returnpath.py)."""
        if not conn.link.wants_to_send():
            return None
        if self._rp_clear_since is None:
            return None
        due = self._rp_clear_since + RETURN_SETTLE_S
        if now + 1e-9 < due:
            return due
        for out in conn.link.frames_for_tx(now, self.channel.config.max_frames):
            self._via_return(rp, out)
        return None

    def return_frame(self, path, frame: bytes) -> None:
        """ReturnPathSink: a frame that came by return path. Only from the
        path's own station to one of ours; then as if heard, marked."""
        key = _key(path.remote)
        parsed = ax25.parse(frame)
        with self.lock:
            if (parsed is None or self.return_paths.get(key) is not path
                    or _key(parsed.source) != key or not self.station.owns(parsed.destination)):
                self.stats["rp_refused"] += 1
                log.warning("return path (%s) for %s: refused %s", path.name, key,
                            parsed.monitor() if parsed is not None else bytes(frame).hex())
                return
            self.stats["rp_heard"] += 1
            self._frame_in(bytes(frame), via=path.name)
            self._update_listening()
        self._wake()

    def return_lost(self, path, reason: str) -> None:
        """ReturnPathSink: the path is down. Links to its station are
        dropped without transmitting and frames to it still queued for
        the radio are discarded (returnpath.py: fail safe)."""
        key = _key(path.remote)
        with self.lock:
            if self.return_paths.get(key) is not path:
                return
            dropped, purged = self._drop_for(key, "return-path-lost")
        log.warning("return path (%s) for %s lost (%s): %d link(s) dropped, %d queued frame(s) discarded",
                    path.name, key, reason, dropped, purged)
        self._emit(Event("return-path", "lost", detail={**path.describe(), "why": reason,
                                                          "links": dropped, "discarded": purged}))
        self._wake()

    def _drop_for(self, key: str, reason: str) -> tuple[int, int]:
        dropped = 0
        for conn in list(self.connections.values()):
            if _key(conn.remote) == key and conn.state is not State.DISCONNECTED:
                conn.link.drop(reason)
                dropped += 1

        def to_them(raw: bytes) -> bool:
            f = ax25.parse(raw)
            return f is not None and _key(f.destination) == key
        return dropped, self.channel.discard(to_them)

    def _tap(self, kind: str, **detail) -> None:
        for tap in list(self.taps):
            try:
                tap(kind, detail)
            except Exception:                       # noqa: BLE001 - an observer never stops the station
                log.exception("packet engine tap failed")

    # -- receiving ----------------------------------------------------------------

    def _frame_in(self, raw: bytes, mode: str | None = None, cn0: float | None = None, *,
                  via: str = "radio", lag: float | None = None) -> None:
        if via == "radio" and self.channel.own_echo(bytes(raw), self.clock()):
            # Our own transmission, heard back by a full-duplex radio
            # (channel.ChannelAccess.own_echo): never a frame from anyone else.
            self.stats["own_echo"] += 1
            return
        frame = ax25.parse(raw)
        if frame is None:
            self.stats["unparsed"] += 1
            return
        self.stats["heard"] += 1
        #: The mode and C/N0 the frame now going to the monitors came in
        #: (an adaptive link's; None otherwise): what a path test counts.
        self.last_heard = (mode, cn0)
        if self.taps:
            self._tap("heard", frame=frame, raw=bytes(raw), via=via, mode=mode, cn0=cn0, decode_lag_s=lag,
                      t=self.clock())
        for monitor in self.monitors:
            monitor(frame)
        if self.digipeater is not None:
            self.digipeater.heard(frame, self.clock())
        if frame.is_ui:
            # UI frames go up whatever their path: APRS reads every packet it
            # hears, direct or not (and deduplicates its own way); IP and ARP
            # skip those still in_transit.
            for handler in self.ui_handlers:
                handler(frame)
            return
        if in_transit(frame):
            return
        local = Address(frame.destination.call, frame.destination.ssid)
        remote = Address(frame.source.call, frame.source.ssid)
        conn = self.connections.get((_key(local), _key(remote)))
        if conn is not None:
            if via == "radio":
                # Our link partner on the air: its over is waited out, not
                # talked over at the busy cap (channel.PARTNER_MAX_WAIT_S).
                self.channel.partner_heard(self.clock())
            if conn.link.modulo == 128:
                frame = ax25.parse(raw, 128)
                if frame is None:
                    self.stats["unparsed"] += 1
                    return
            conn.link.receive(frame, mode, cn0)
            return
        if not self.station.owns(local):
            return
        self._unconnected(frame, local, remote)

    def _unconnected(self, frame: Frame, local: Address, remote: Address) -> None:
        """A frame to one of our addresses from a station with no link."""
        path = [Address(h.call, h.ssid) for h in reversed(frame.via)]
        kind = frame.type
        if kind is FrameType.SABME and not self.link_config.v22:
            self.stats["ignored_sabme"] += 1
            return
        if kind in (FrameType.SABM, FrameType.SABME):
            call = IncomingCall(local, remote, path, 128 if kind is FrameType.SABME else 8)
            for address, accept in list(self.listeners):
                if address != _key(local):
                    continue
                setup = accept(call)
                if setup:
                    conn = self._new_connection(local, remote, path, self.link_config)
                    if callable(setup):
                        setup(conn)
                    conn.link.receive(frame)
                    self._emit(Event("incoming", "connected-by-peer", conn))
                    return
            self.stats["refused"] += 1
            self._reply(local, remote, path, Control(FrameType.DM, frame.control.pf))
            return
        if kind in (FrameType.XID, FrameType.TEST) and frame.command is not False:
            # Connectionless XID and TEST are answered by a link that never
            # connects.
            link = DataLink(local, remote, path, self.link_config, send=functools.partial(self._send_to, _key(remote)),
                            now=self.clock)
            link.receive(frame)
            return
        if kind is FrameType.DISC or (frame.control.pf and frame.command is not False):
            self._reply(local, remote, path, Control(FrameType.DM, frame.control.pf))

    def _reply(self, local, remote, path, control: Control) -> None:
        frame = Frame.build(local, remote, control, via=path, command=False)
        self._send_to(_key(remote), Outgoing(frame.encode()))

    def _packet_from_link(self, conn: Connection, pid: int, data: bytes) -> None:
        handler = self.packet_handlers.get(pid)
        if handler is not None:
            handler(conn, pid, data)
        else:
            self.stats["unhandled_pid"] += 1

    # -- events --------------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        if self.taps:
            self._tap("event", event=event)

    def _refused(self, reason: str, frames: int) -> None:
        self._emit(Event("tx-refused", reason, detail={"frames": frames}))

    def _transmitted(self, start: float, end: float, frames: list[bytes]) -> None:
        if self.digipeater is not None:
            self.digipeater.sent(frames, start)
        if self.taps:
            self._tap("transmitted", start=start, end=end, frames=list(frames))

    # -- time ------------------------------------------------------------------------

    def poll(self, now: float | None = None) -> float | None:
        """Do what is due; returns when to be called next (None: on input)."""
        with self._polling:
            with self.lock:
                now = self.clock() if now is None else now
                due = []
                if self.channel.dcd or self.channel.radio_busy:
                    self._rp_clear_since = None
                elif self._rp_clear_since is None:
                    self._rp_clear_since = now
                for conn in list(self.connections.values()):
                    conn.link.poll(now)
                    d = conn.link.next_deadline()
                    if d is not None:
                        due.append(d)
                    rp = self._outbound(_key(conn.remote)) if self.return_paths else None
                    if rp is not None:
                        d = self._return_answers(conn, rp, now)
                        if d is not None:
                            due.append(d)
                self._update_listening()
                for service in self.services:
                    service.poll(now)
                    d = service.next_deadline()
                    if d is not None:
                        due.append(d)
                d, plan = self.channel.decide(now)
            if plan is not None:
                # Keying waits on the radio's thread, which may be waiting
                # for this lock to hand in audio: never under it.
                self.channel.act(plan)
                with self.lock:
                    d = self.channel.finish(plan)
            if d is not None:
                due.append(d)
            return min(due) if due else None

    def _wake(self) -> None:
        self._wake_event.set()


class Runner:
    """Polls an engine against the wall clock in a thread of its own: the
    app's (and the live direwolf test's) way to run one. Input from the
    radio's threads wakes it."""

    def __init__(self, engine: Engine, max_sleep_s: float = 0.05):
        self.engine = engine
        self.max_sleep_s = max_sleep_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="packet-engine", daemon=True)

    def start(self) -> Runner:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.engine._wake()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        engine = self.engine
        while not self._stop.is_set():
            try:
                nxt = engine.poll()
            except Exception:                       # noqa: BLE001
                # A bug in one service must not silence the station: the
                # radio would keep receiving into an engine nobody polls.
                log.exception("packet engine poll failed")
                nxt = None
            wait = self.max_sleep_s if nxt is None else max(0.0, min(self.max_sleep_s, nxt - engine.clock()))
            engine._wake_event.wait(wait)
            engine._wake_event.clear()
