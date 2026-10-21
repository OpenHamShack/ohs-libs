"""A conference room: several connected stations, every line one of them
types sent to all the others with the sender's call in front, as on a
node's CONF or a BBS's chat. One implementation for the desktop app's
hosted room and the controller's always-on room.

    room = Room("GM4BRI-2", clock=engine.clock)
    RoomService(engine, room, ssid=2)       # stations connect to GM4BRI-2 to join
    room.join(member)                       # or a BBS session moves a caller in (bbs.py)

**What members see** (every line ends in CR):

    *** Welcome to the GM4BRI-2 conference. 3 here: G0ABC G4XYZ M0DEF.
    *** Topic: Sunday net
    G4XYZ: anyone on 23 cm tonight?
    * M0DEF waves
    *** G0ABC joined
    *** G0ABC left

**Commands** start with `/`: `/who`, `/quit` (`/q`, `/bye`, `/exit`),
`/me action`, `/topic [text]`, `/help` (`/h`, `/?`). Anything else is a
line for the room. A member's own lines are not echoed back.

**Limits**, each a `RoomConfig` field: at most `max_members`; a member
silent for `idle_s` is dropped; more than `flood_lines` lines in
`flood_s` seconds are dropped (the member is told once per burst); the
last `history` lines are shown to whoever joins. The room keeps nothing
on disk.

A member is anything with `call` and `send(text)`; `ConnectionMember`
is one on an AX.25 connection. Text for people goes through gettext
(i18n.py), as the mailbox's does.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Callable

from .fbb import LineBuffer
from .i18n import _

log = logging.getLogger(__name__)

QUIT = {"/QUIT", "/Q", "/BYE", "/EXIT"}
HELP = {"/HELP", "/H", "/?"}


@dataclass
class RoomConfig:
    max_members: int = 20
    idle_s: float = 1800.0
    flood_lines: int = 10
    flood_s: float = 10.0
    history: int = 10
    max_line: int = 250


class Member:
    """Someone in the room. `left(reason)` is called when the room lets
    them go (quit, idle, removed), so the owner of the link can close it
    or take the station back to a BBS prompt."""

    def __init__(self, call: str, send: Callable[[str], None], left: Callable[[str], None] | None = None):
        self.call = call.upper()
        self._send = send
        self._left = left
        self.last_input = 0.0
        self.joined_at = 0.0
        self._recent: collections.deque[float] = collections.deque()
        self._told_flood = False

    def send(self, text: str) -> None:
        self._send(text)

    def left(self, reason: str) -> None:
        if self._left is not None:
            self._left(reason)


class Room:
    def __init__(self, name: str, *, clock, config: RoomConfig | None = None, topic: str = ""):
        self.name = name.upper()
        self.clock = clock
        self.config = config or RoomConfig()
        self.topic = topic
        self.members: list[Member] = []
        self.history: collections.deque[str] = collections.deque()
        #: Every line of the room as (time, text) for the sysop page; bounded.
        self.log: collections.deque[tuple[float, str]] = collections.deque(maxlen=500)
        #: Told of each room event: (kind, call, text); kind joined, left, line, topic.
        self.observers: list[Callable[[str, str, str], None]] = []

    # ----------------------------------------------------------- membership
    def calls(self) -> list[str]:
        return [m.call for m in self.members]

    def join(self, member: Member) -> bool:
        if len(self.members) >= self.config.max_members:
            member.send(_("*** The {room} conference is full ({n} stations). Try again later.").format(
                room=self.name, n=len(self.members)))
            return False
        now = self.clock()
        member.last_input = member.joined_at = now
        self.members.append(member)
        here = " ".join(self.calls())
        member.send(_("*** Welcome to the {room} conference. {n} here: {calls}.").format(
            room=self.name, n=len(self.members), calls=here))
        if self.topic:
            member.send(_("*** Topic: {topic}").format(topic=self.topic))
        for line in self.history:
            member.send(line)
        member.send(_("*** /help for the commands, /quit to leave."))
        self._broadcast(_("*** {call} joined").format(call=member.call), but=member, record=False)
        self._event("joined", member.call, "")
        return True

    def leave(self, member: Member, reason: str = "quit") -> None:
        if member not in self.members:
            return
        self.members.remove(member)
        self._broadcast(_("*** {call} left").format(call=member.call), record=False)
        self._event("left", member.call, reason)
        if reason != "disconnected":
            member.left(reason)

    def remove(self, call: str, by: str = "sysop") -> bool:
        """The sysop takes a station out of the room."""
        for m in list(self.members):
            if m.call == call.upper():
                m.send(_("*** You have been removed from the conference by the {by}.").format(by=by))
                self.leave(m, "removed")
                return True
        return False

    # ---------------------------------------------------------------- lines
    def line(self, member: Member, text: str) -> None:
        text = text.strip()[: self.config.max_line]
        if not text or member not in self.members:
            return
        now = self.clock()
        member.last_input = now
        word = text.split()[0].upper()
        if word.startswith("/"):
            self._command(member, word, text[len(word):].strip())
            return
        if self._flooding(member, now):
            return
        self._broadcast(f"{member.call}: {text}", but=member)
        self._event("line", member.call, text)

    def _flooding(self, member: Member, now: float) -> bool:
        recent = member._recent
        while recent and now - recent[0] > self.config.flood_s:
            recent.popleft()
        if len(recent) >= self.config.flood_lines:
            if not member._told_flood:
                member._told_flood = True
                member.send(_("*** Too many lines too fast; some were not sent."))
            return True
        recent.append(now)
        member._told_flood = False
        return False

    def _command(self, member: Member, word: str, rest: str) -> None:
        if word in QUIT:
            member.send(_("*** You have left the conference."))
            self.leave(member, "quit")
        elif word == "/WHO":
            member.send(_("*** {n} here: {calls}").format(n=len(self.members), calls=" ".join(self.calls())))
        elif word == "/ME":
            if rest and not self._flooding(member, self.clock()):
                self._broadcast(f"* {member.call} {rest}", but=member)
                self._event("line", member.call, f"/me {rest}")
        elif word == "/TOPIC":
            if rest:
                self.set_topic(rest, by=member.call)
            else:
                member.send(_("*** Topic: {topic}").format(topic=self.topic) if self.topic
                            else _("*** No topic is set."))
        elif word in HELP:
            member.send(_("*** /who - who is here"))
            member.send(_("*** /me text - an action"))
            member.send(_("*** /topic [text] - show or set the topic"))
            member.send(_("*** /quit - leave"))
        else:
            member.send(_("*** Unknown command {word}. /help for the commands.").format(word=word.lower()))

    def set_topic(self, topic: str, by: str = "SYSOP") -> None:
        self.topic = topic.strip()[: self.config.max_line]
        self._broadcast(_("*** {call} set the topic: {topic}").format(call=by.upper(), topic=self.topic),
                        record=False)
        self._event("topic", by.upper(), self.topic)

    def say(self, text: str, call: str = "SYSOP") -> None:
        """A line from someone not in the room (the sysop's page)."""
        text = text.strip()[: self.config.max_line]
        if text:
            self._broadcast(f"{call.upper()}: {text}")
            self._event("line", call.upper(), text)

    def _broadcast(self, text: str, but: Member | None = None, record: bool = True) -> None:
        if record:
            self.history.append(text)
            while len(self.history) > self.config.history:
                self.history.popleft()
        self.log.append((self.clock(), text))
        for m in list(self.members):
            if m is not but:
                try:
                    m.send(text)
                except Exception:                   # noqa: BLE001 - one dead link must not silence the room
                    log.exception("conference %s: sending to %s failed", self.name, m.call)

    def _event(self, kind: str, call: str, text: str) -> None:
        for fn in list(self.observers):
            try:
                fn(kind, call, text)
            except Exception:                       # noqa: BLE001
                log.exception("conference observer failed")

    # --------------------------------------------------------------- timers
    def poll(self, now: float) -> None:
        for m in list(self.members):
            if now - m.last_input >= self.config.idle_s:
                m.send(_("*** No input for a while; you have left the conference."))
                self.leave(m, "idle")

    def next_deadline(self) -> float | None:
        if not self.members:
            return None
        return min(m.last_input for m in self.members) + self.config.idle_s


class ConnectionMember(Member):
    """A member on an AX.25 connection: lines out with CR, lines in cut by
    LineBuffer. `on_left` is told when the room lets them go; by default
    the link is closed."""

    def __init__(self, conn, room: Room, on_left: Callable[[str], None] | None = None):
        super().__init__(conn.remote.call if conn.remote.ssid == 0 else f"{conn.remote.call}-{conn.remote.ssid}",
                         lambda text: conn.send((text + "\r").encode("utf-8")),
                         on_left or (lambda reason: conn.close()))
        self.conn = conn
        self.room = room
        self.lines = LineBuffer(max_line=room.config.max_line + 50)

    def data(self, chunk: bytes) -> None:
        for line in self.lines.feed(chunk):
            self.room.line(self, line.replace("\x1a", ""))


class RoomService:
    """The room on one of the station's SSIDs: a station that connects
    there is in the room at once, and leaves it by /quit or by
    disconnecting. Off for a station that is barred (`allow`)."""

    def __init__(self, engine, room: Room, ssid: int | None, *, allow: Callable[[str], bool] = lambda call: True):
        self.engine = engine
        self.room = room
        self.allow = allow
        self.address = engine.station.address(ssid)
        engine.listen(self.address, self._accept)
        engine.services.append(room)

    def _accept(self, call):
        if not self.allow(str(call.remote)):
            return None
        return self._open

    def _open(self, conn) -> None:
        member = ConnectionMember(conn, self.room)
        conn.on_data = member.data
        previous = conn.on_event

        def on_event(event, previous=previous):
            if event.kind == "connected":
                self.room.join(member) or conn.close()
            elif event.kind == "disconnected":
                self.room.leave(member, "disconnected")
            if previous is not None:
                previous(event)
        conn.on_event = on_event
