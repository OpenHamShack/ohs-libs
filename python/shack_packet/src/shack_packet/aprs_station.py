"""The APRS side of a station: beacons, messages, and who has been heard.

    aprs = AprsStation(engine, AprsConfig(beacons=[Beacon(position_text, every_s=1800)]))
    msg = aprs.send_message("G0ABC-9", "QSY 145.500?")
    aprs.on_message = lambda m: show(m.source, m.text)

**Beacons** go out on their schedule with limits a fixed station on a busy
frequency should keep: no beacon of this station's within `min_gap_s` of
another (60 s), none more often than `min_interval_s` (5 minutes; the
UK network's guidance for fixed stations is nearer 30), and the first a
random 10-60 s after start so a room full of restarted stations does not
beacon in step. A moving station's position beacon (`Beacon.source`, a
callable giving the position, speed and course) follows SmartBeaconing
(HamHUD's): slow when slow, faster with speed, and at once on a turn
sharper than the speed allows - within the same floors.

**Messages** (APRS 1.1 §14) are numbered, sent, and sent again until
acknowledged: after 30, 60, 120, 240 and 480 s (each wait doubles, as
APRSIS32 and direwolf's clients do), then given up. One message per
addressee is outstanding at a time, the rest queue behind it. An ack or a
rej for the number ends it; a reply-ack (`{MM}AA`, APRS 1.2) is read too.
Text longer than 67 characters is sent as several messages, cut at words.
Every numbered message to this station is acknowledged, duplicates
included (the sender is retrying because it missed our ack), but handed to
the app once: a message is a duplicate of one heard from the same station
with the same number (or, unnumbered, the same text) within `dedupe_s`.

**Heard** is every station heard on the channel, with when, how often,
whether direct or through a digipeater (ax25.Frame.heard_from), and the
last position, status and symbol it sent.

Events reach the engine's on_event with these keys (for the app to
translate): EVENTS below.
"""

from __future__ import annotations

import collections
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from . import aprs
from .aprs import TOCALL  # noqa: F401 - re-exported: AprsConfig's default, and older callers' name
from .ax25 import Frame
from .engine import Event

EVENTS = {
    "aprs-message": "a message to this station arrived",
    "aprs-acked": "a message we sent was acknowledged",
    "aprs-rejected": "a message we sent was rejected by the other station",
    "aprs-timeout": "a message we sent was never acknowledged",
    "aprs-beacon": "a beacon went out",
}

RETRY_S = (30.0, 60.0, 120.0, 240.0, 480.0)

_REPLY_ACK = re.compile(r"\{([A-Za-z0-9]{1,5})\}([A-Za-z0-9]{0,5})$")


@dataclass
class SmartBeaconing:
    """HamHUD's SmartBeaconing, its usual parameters (km/h, seconds,
    degrees)."""
    fast_speed: float = 90.0
    fast_rate: float = 60.0
    slow_speed: float = 5.0
    slow_rate: float = 1800.0
    turn_min: float = 28.0
    turn_slope: float = 255.0       # degrees x mph, as the original
    turn_time: float = 30.0

    def interval(self, speed_kmh: float) -> float:
        if speed_kmh <= self.slow_speed:
            return self.slow_rate
        if speed_kmh >= self.fast_speed:
            return self.fast_rate
        return self.fast_rate * self.fast_speed / speed_kmh

    def turn_threshold(self, speed_kmh: float) -> float:
        mph = max(0.1, speed_kmh / 1.609344)
        return self.turn_min + self.turn_slope / mph


@dataclass
class Fix:
    lat: float
    lon: float
    speed_kmh: float = 0.0
    course_deg: float | None = None


@dataclass
class Beacon:
    """One scheduled transmission. `text` is the information field (from
    aprs.format_position or format_status), or None with a `source` for a
    moving station's position (then `symbol` and `comment` build it)."""
    text: str | None = None
    every_s: float = 1800.0
    via: list[str] = field(default_factory=lambda: ["WIDE1-1", "WIDE2-1"])
    source: Callable[[], Fix | None] | None = None
    smart: SmartBeaconing | None = None
    symbol: str = "/>"
    comment: str = ""


@dataclass
class AprsConfig:
    beacons: list[Beacon] = field(default_factory=list)
    min_interval_s: float = 300.0
    min_gap_s: float = 60.0
    first_delay_s: tuple[float, float] = (10.0, 60.0)
    message_via: list[str] = field(default_factory=lambda: ["WIDE1-1", "WIDE2-1"])
    retry_s: tuple[float, ...] = RETRY_S
    dedupe_s: float = 1800.0
    #: Further addressees this station answers to (a tactical call).
    also_me: list[str] = field(default_factory=list)
    heard_max: int = 2000
    tocall: str = TOCALL


@dataclass
class OutgoingMessage:
    addressee: str
    text: str
    id: str
    state: str = "queued"          # queued, sent, acked, rejected, timeout
    tries: int = 0
    next_s: float | None = None


@dataclass
class IncomingMessage:
    source: str
    addressee: str
    text: str
    id: str
    heard_s: float
    path: list[str]


@dataclass
class Heard:
    call: str
    first_s: float
    last_s: float
    packets: int = 0
    direct: bool = False
    heard_from: str = ""
    path: list[str] = field(default_factory=list)
    position: aprs.Position | None = None
    symbol: str = ""
    status: str = ""
    comment: str = ""
    last: str = ""


@dataclass
class _Scheduled:
    beacon: Beacon
    due: float
    last_sent: float | None = None
    last_course: float | None = None


class AprsStation:
    def __init__(self, engine, config: AprsConfig | None = None, rng: random.Random | None = None):
        self.engine = engine
        self.config = config or AprsConfig()
        self.rng = rng or engine.rng
        now = engine.clock()
        lo, hi = self.config.first_delay_s
        self._beacons = [_Scheduled(b, now + self.rng.uniform(lo, hi)) for b in self.config.beacons]
        self._last_beacon: float | None = None
        self._next_id = self.rng.randrange(1, 900)
        self.outgoing: list[OutgoingMessage] = []
        self.heard: collections.OrderedDict[str, Heard] = collections.OrderedDict()
        self._seen: collections.OrderedDict[tuple, float] = collections.OrderedDict()
        self.on_message: Callable[[IncomingMessage], None] | None = None
        self.stats = collections.Counter()
        engine.ui_handlers.append(self._ui)
        engine.monitors.append(self._heard)
        engine.services.append(self)

    # -- who we are -----------------------------------------------------------------

    def _me(self) -> set[str]:
        me = {str(self.engine.station.address())}
        return me | {c.upper() for c in self.config.also_me}

    # -- beacons ------------------------------------------------------------------------

    def add_beacon(self, beacon: Beacon) -> None:
        lo, hi = self.config.first_delay_s
        self._beacons.append(_Scheduled(beacon, self.engine.clock() + self.rng.uniform(lo, hi)))

    def _beacon_text(self, s: _Scheduled, now: float) -> tuple[str | None, float]:
        """What to send now (None: nothing), and when to look again."""
        b, cfg = s.beacon, self.config
        if b.source is None:
            return b.text, max(b.every_s, cfg.min_interval_s)
        fix = b.source()
        if fix is None:
            return None, 30.0
        smart = b.smart or SmartBeaconing()
        interval = max(smart.interval(fix.speed_kmh), cfg.min_gap_s)
        text = aprs.format_position(fix.lat, fix.lon, b.symbol, b.comment, course_deg=fix.course_deg,
                                    speed_kmh=fix.speed_kmh if fix.course_deg is not None else None)
        return text, interval

    def _beacon_due(self, s: _Scheduled, now: float) -> bool:
        if now + 1e-9 >= s.due:
            return True
        b = s.beacon
        if b.source is None or s.last_sent is None:
            return False
        # Corner pegging: a turn sharper than the speed allows, once
        # turn_time has passed since the last beacon.
        fix = b.source()
        smart = b.smart or SmartBeaconing()
        if fix is None or fix.course_deg is None or s.last_course is None or fix.speed_kmh <= smart.slow_speed:
            return False
        turned = abs((fix.course_deg - s.last_course + 180.0) % 360.0 - 180.0)
        return turned > smart.turn_threshold(fix.speed_kmh) and now - s.last_sent >= smart.turn_time

    def _send_beacons(self, now: float) -> None:
        for s in self._beacons:
            if not self._beacon_due(s, now):
                continue
            if self._last_beacon is not None and now - self._last_beacon < self.config.min_gap_s:
                s.due = self._last_beacon + self.config.min_gap_s
                continue
            text, interval = self._beacon_text(s, now)
            s.due = now + interval
            if text is None:
                continue
            self.engine.send_ui(self.config.tocall, text, via=s.beacon.via)
            s.last_sent = self._last_beacon = now
            if s.beacon.source is not None:
                fix = s.beacon.source()
                s.last_course = fix.course_deg if fix is not None else None
            self.stats["beacons"] += 1
            self.engine._emit(Event("aprs-beacon", "aprs-beacon", detail={"text": text}))

    # -- messages -----------------------------------------------------------------------

    def send_message(self, addressee: str, text: str) -> list[OutgoingMessage]:
        """Queue a message (several, if it is long); returns them, to watch
        their `state`."""
        out = []
        with self.engine.lock:
            for part in aprs.clean_message(text):
                msg = OutgoingMessage(addressee.strip().upper(), part, self._new_id())
                self.outgoing.append(msg)
                out.append(msg)
        self.engine._wake()
        return out

    def _new_id(self) -> str:
        ident = str(self._next_id)
        self._next_id = self._next_id % 99999 + 1
        return ident

    def _send_messages(self, now: float) -> None:
        busy = {m.addressee for m in self.outgoing if m.state == "sent"}
        for msg in self.outgoing:
            if msg.state == "queued" and msg.addressee not in busy:
                msg.state = "sent"
                msg.next_s = now
                busy.add(msg.addressee)
            if msg.state != "sent" or msg.next_s is None or now + 1e-9 < msg.next_s:
                continue
            if msg.tries > len(self.config.retry_s):
                msg.state = "timeout"
                self.engine._emit(Event("aprs-timeout", "aprs-timeout", detail={"message": msg}))
                continue
            self.engine.send_ui(self.config.tocall, aprs.format_message(msg.addressee, msg.text, msg.id),
                                via=self.config.message_via)
            self.stats["messages_sent"] += 1
            wait = self.config.retry_s[msg.tries] if msg.tries < len(self.config.retry_s) else self.config.retry_s[-1]
            msg.tries += 1
            msg.next_s = now + wait
        self.outgoing = [m for m in self.outgoing if m.state in ("queued", "sent")] + \
            [m for m in self.outgoing if m.state not in ("queued", "sent")][-100:]

    def _acked(self, source: str, ident: str, reject: bool) -> None:
        for msg in self.outgoing:
            if msg.state == "sent" and msg.id == ident and msg.addressee == source:
                msg.state = "rejected" if reject else "acked"
                kind = "aprs-rejected" if reject else "aprs-acked"
                self.engine._emit(Event(kind, kind, detail={"message": msg}))
                self.engine._wake()
                return

    # -- what we hear ------------------------------------------------------------------

    def _ui(self, frame: Frame) -> None:
        if frame.pid != 0xF0:
            return
        packet = aprs.parse(frame)
        source = str(frame.source)
        self._note(frame, packet)
        if packet.kind not in ("message", "ack", "reject"):
            return
        if packet.addressee.upper() not in self._me():
            return
        if packet.kind in ("ack", "reject"):
            self._acked(source, packet.message_id, packet.kind == "reject")
            return
        text, ident = packet.message, packet.message_id
        reply = _REPLY_ACK.search(text)
        if reply:
            ident = reply.group(1)
            text = text[:reply.start()]
            if reply.group(2):
                self._acked(source, reply.group(2), False)
        if ident:
            # Always acknowledged: a duplicate means our last ack was lost.
            self.engine.send_ui(self.config.tocall, aprs.format_ack(source, ident), via=self.config.message_via)
            self.stats["acks_sent"] += 1
        now = self.engine.clock()
        key = (source, ident) if ident else (source, text)
        horizon = now - self.config.dedupe_s
        while self._seen and next(iter(self._seen.values())) < horizon:
            self._seen.popitem(last=False)
        if key in self._seen:
            self.stats["duplicates"] += 1
            return
        self._seen[key] = now
        message = IncomingMessage(source, packet.addressee, text, ident, now, frame.path)
        self.stats["messages_received"] += 1
        if self.on_message is not None:
            self.on_message(message)
        self.engine._emit(Event("aprs-message", "aprs-message", detail={"message": message}))

    def _heard(self, frame: Frame) -> None:
        now = self.engine.clock()
        call = str(frame.source)
        entry = self.heard.pop(call, None) or Heard(call, now, now)
        entry.last_s = now
        entry.packets += 1
        entry.heard_from = frame.heard_from
        entry.direct = not frame.digipeated
        entry.path = frame.path
        entry.last = frame.monitor()
        self.heard[call] = entry
        while len(self.heard) > self.config.heard_max:
            self.heard.popitem(last=False)

    def hear(self, frame: Frame, packet: aprs.Packet | None = None) -> None:
        """A frame heard by another receiver (the SDR monitor, sdr/monitor.py
        feed_heard): into the heard table, with its position, and nothing
        else. Never the message path, which acknowledges: a message the SDR
        heard addressed to this station must not make it transmit - the SDR
        may be listening to a channel this station's radio is not on."""
        self._heard(frame)
        if frame.pid == 0xF0:
            self._note(frame, packet if packet is not None else aprs.parse(frame))

    def _note(self, frame: Frame, packet: aprs.Packet) -> None:
        entry = self.heard.get(str(frame.source))
        if entry is None:
            return
        if packet.position is not None:
            entry.position = packet.position
            entry.symbol = packet.position.symbol
            entry.comment = packet.comment
        if packet.kind == "status":
            entry.status = packet.status

    # -- time -------------------------------------------------------------------------

    def poll(self, now: float) -> None:
        self._send_beacons(now)
        self._send_messages(now)

    def next_deadline(self) -> float | None:
        due = [s.due for s in self._beacons]
        due += [m.next_s for m in self.outgoing if m.state == "sent" and m.next_s is not None]
        busy = {m.addressee for m in self.outgoing if m.state == "sent"}
        if any(m.state == "queued" and m.addressee not in busy for m in self.outgoing):
            due.append(self.engine.clock())
        if any(s.beacon.source is not None for s in self._beacons):
            # A moving station looks at its course for corner pegging.
            due.append(self.engine.clock() + 5.0)
        return min(due) if due else None

