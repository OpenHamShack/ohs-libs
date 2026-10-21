"""A personal mailbox: stations that connect to this station's callsign
can leave a message for the operator, and read what the operator left
for them. A PMS, in the old TNC sense, not a BBS: no forwarding, no
bulletins, one owner.

Off unless the operator allows it (`MailboxConfig.enabled`), and even then
`allow(callsign)` can refuse a caller; refused callers get DM like any
station that nobody is listening for (engine.py).

**The command set** is the one a packet user already knows from a TNC's
mailbox, one letter or the word, case ignored, lines ending in CR, LF or
both:

    L / LIST           messages for you (or that you left), numbered
    R n / READ n       read one
    S [CALL] / SEND    leave a message: for the owner unless CALL is given
                       (only the owner, unless third-party mail is allowed);
                       a subject line, then the text, ended by /EX (or Ctrl-Z)
                       on a line of its own; /ABORT gives up
    K n / KILL n       delete one of yours
    H / HELP / ?       the commands
    B / BYE / Q        disconnect

**Mail from another OpenHamShack station** (0.3.0): the mailbox greets
with an FBB SID line, and a caller whose first line is a SID with `F`
forwards instead of typing - fbb.py's exchange, FORWARDING.md §9 - so a
station's store-and-forward client (stationmail.py) can deliver straight
to its addressee. It takes personal mail for the owner only (or anyone,
with third-party mail allowed), within the same per-caller limits as a
typed message, and offers the caller what the owner left for it. A BID it
has taken is remembered, so mail offered again is answered "have it".

A caller sees only messages addressed to it or that it sent; the owner
reads everything through the store (the GUI comes later). Every limit is a
setting: the size of one message, how many one caller may leave, how long
a silent caller is kept connected.

Everything a caller reads goes through gettext (i18n.py): it is text for a
person, sent over the air in the operator's language.

**Storage** is behind `Store`: `MemoryStore` for tests and a node that
keeps nothing, `FileStore` for the desktop app - one JSON file, written to
a temporary file and renamed into place, so a crash mid-write leaves the
old mailbox rather than half a new one.
"""

from __future__ import annotations

import abc
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import fbb
from .engine import Event, IncomingCall
from .i18n import _

#: BIDs a mailbox remembers (the newest), so a resent message is not kept twice.
BID_MEMORY = 500


@dataclass
class Message:
    id: int
    sender: str
    to: str
    subject: str
    body: str
    time: float
    read: bool = False


class Store(abc.ABC):
    @abc.abstractmethod
    def add(self, sender: str, to: str, subject: str, body: str, when: float) -> Message: ...

    @abc.abstractmethod
    def all(self) -> list[Message]: ...

    @abc.abstractmethod
    def delete(self, ident: int) -> bool: ...

    @abc.abstractmethod
    def mark_read(self, ident: int) -> None: ...

    def get(self, ident: int) -> Message | None:
        return next((m for m in self.all() if m.id == ident), None)

    def for_station(self, call: str) -> list[Message]:
        call = call.upper()
        return [m for m in self.all() if m.to == call or m.sender == call]

    # Forwarded mail's BIDs (FORWARDING.md §6). Kept beside the messages,
    # not in them, so a file written here still opens in 0.2's mailbox.
    def has_bid(self, bid: str) -> bool:
        return bid.upper() in getattr(self, "_bids", [])

    def remember_bid(self, bid: str) -> None:
        bids = [b for b in getattr(self, "_bids", []) if b != bid.upper()] + [bid.upper()]
        self._bids = bids[-BID_MEMORY:]


class MemoryStore(Store):
    def __init__(self):
        self._messages: list[Message] = []
        self._next = 1

    def add(self, sender, to, subject, body, when) -> Message:
        msg = Message(self._next, sender.upper(), to.upper(), subject, body, when)
        self._next += 1
        self._messages.append(msg)
        return msg

    def all(self) -> list[Message]:
        return list(self._messages)

    def delete(self, ident: int) -> bool:
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.id != ident]
        return len(self._messages) != before

    def mark_read(self, ident: int) -> None:
        for m in self._messages:
            if m.id == ident:
                m.read = True


class FileStore(MemoryStore):
    """MemoryStore, saved to `path` (JSON) after every change."""

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        # Saved from two threads at once: the window marks a message read
        # while the engine's thread files one arriving. Both wrote the same
        # .tmp and one os.replace found it already moved; in a Qt slot that
        # unhandled FileNotFoundError aborted the whole app (seen as an
        # intermittent "Fatal Python error: Aborted" in Packet Radio's
        # test_ui, 20/10/2026). One writer at a time.
        self._save_lock = threading.Lock()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = set(Message.__dataclass_fields__)
            self._messages = [Message(**{k: v for k, v in m.items() if k in known})
                              for m in data.get("messages", [])]
            self._next = int(data.get("next", 1))
            self._bids = [str(b) for b in data.get("bids", [])]

    def _save(self) -> None:
        with self._save_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"next": self._next, "messages": [asdict(m) for m in self._messages],
                                       "bids": getattr(self, "_bids", [])}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    def add(self, *args, **kw) -> Message:
        msg = super().add(*args, **kw)
        self._save()
        return msg

    def delete(self, ident: int) -> bool:
        gone = super().delete(ident)
        if gone:
            self._save()
        return gone

    def mark_read(self, ident: int) -> None:
        super().mark_read(ident)
        self._save()

    def remember_bid(self, bid: str) -> None:
        super().remember_bid(bid)
        self._save()


@dataclass
class MailboxConfig:
    enabled: bool = False
    #: The SSID it answers on; None for the station's own address.
    ssid: int | None = None
    allow: Callable[[str], bool] = field(default=lambda call: True)
    allow_third_party: bool = False
    max_message_bytes: int = 8000
    max_per_caller: int = 20
    idle_timeout_s: float = 600.0
    #: In the FBB SID the mailbox greets with.
    version: str = "0.3.0"


_END = ("/EX", "\x1a")


class _Session:
    def __init__(self, mailbox: Mailbox, conn):
        self.mailbox = mailbox
        self.conn = conn
        # Mail is for the operator, not the SSID he happened to connect
        # from: G0ABC-9 in the car reads what was left for G0ABC.
        self.caller = conn.remote.call
        self.buffer = b""
        self.state = "command"
        self.draft: dict = {}
        self.last_input = mailbox.engine.clock()
        self.first = True
        self.exchange: fbb.Exchange | None = None
        self.lines = fbb.LineBuffer()
        conn.on_data = self.data

    def say(self, *lines: str) -> None:
        self.conn.send(("\r".join(lines) + "\r").encode("utf-8"))

    def prompt(self) -> None:
        self.conn.send(b">\r")

    def data(self, data: bytes) -> None:
        self.last_input = self.mailbox.engine.clock()
        if self.exchange is not None:
            for line in self.lines.feed(data):
                self.exchange.line(line)
            return
        self.buffer += data
        while True:
            cut = [i for i in (self.buffer.find(b"\r"), self.buffer.find(b"\n")) if i >= 0]
            if not cut:
                if b"\x1a" in self.buffer:
                    cut = [self.buffer.find(b"\x1a")]
                    line, self.buffer = self.buffer[:cut[0]] + b"\x1a", self.buffer[cut[0] + 1:]
                    self.line(self._text(line))
                    continue
                if len(self.buffer) > 4096:
                    line, self.buffer = self.buffer, b""
                    self.line(self._text(line))
                return
            at = min(cut)
            line = self.buffer[:at]
            rest = self.buffer[at + 1:]
            if self.buffer[at:at + 2] in (b"\r\n", b"\n\r"):
                rest = self.buffer[at + 2:]
            self.buffer = rest
            self.line(self._text(line))

    @staticmethod
    def _text(raw: bytes) -> str:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return "".join(ch for ch in text if ch >= " " or ch in "\t\x1a")

    # -- the commands ----------------------------------------------------------------

    def line(self, text: str) -> None:
        if self.first and text.strip():
            self.first = False
            got = fbb.parse_sid(text)
            if got is not None:
                self.forward(got)
                return
        if self.exchange is not None:
            self.exchange.line(text)
            return
        if self.state == "subject":
            self.draft["subject"] = text.strip()[:80]
            self.state = "body"
            self.draft["body"] = []
            self.say(_("Enter your message. End it with /EX on a line of its own, or /ABORT to give up."))
            return
        if self.state == "body":
            self.body_line(text)
            return
        words = text.strip().split()
        if not words:
            self.prompt()
            return
        verb, args = words[0].upper(), words[1:]
        handler = {"L": self.list, "LIST": self.list, "R": self.read, "READ": self.read,
                   "S": self.send, "SEND": self.send, "SP": self.send, "K": self.kill, "KILL": self.kill,
                   "H": self.help, "HELP": self.help, "?": self.help,
                   "B": self.bye, "BYE": self.bye, "Q": self.bye, "QUIT": self.bye}.get(verb)
        if handler is None:
            self.say(_("Unknown command. H for help."))
            self.prompt()
            return
        handler(args)

    def help(self, args) -> None:
        self.say(_("L - list your messages"),
                 _("R n - read message n"),
                 _("S - leave a message for {owner}").format(owner=self.mailbox.owner),
                 _("K n - delete message n"),
                 _("B - disconnect"))
        self.prompt()

    def _mine(self) -> list[Message]:
        return self.mailbox.store.for_station(self.caller)

    def list(self, args) -> None:
        mine = self._mine()
        if not mine:
            self.say(_("No messages."))
        for m in mine:
            when = time.strftime("%d/%m %H:%M", time.gmtime(m.time))
            mark = "" if m.read or m.sender == self.caller else "*"
            self.say(_("{n}{mark} {when} from {sender} to {to}: {subject}").format(
                n=m.id, mark=mark, when=when, sender=m.sender, to=m.to, subject=m.subject))
        self.prompt()

    def _number(self, args) -> Message | None:
        if len(args) != 1 or not args[0].isdigit():
            self.say(_("Which message? Give its number, as L lists them."))
            return None
        msg = self.mailbox.store.get(int(args[0]))
        if msg is None or msg not in self._mine():
            self.say(_("There is no message {n} for you.").format(n=args[0]))
            return None
        return msg

    def read(self, args) -> None:
        msg = self._number(args)
        if msg is not None:
            when = time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime(msg.time))
            self.say(_("Message {n} from {sender} to {to}, {when}").format(
                n=msg.id, sender=msg.sender, to=msg.to, when=when),
                _("Subject: {subject}").format(subject=msg.subject), "", *msg.body.split("\n"))
            if msg.to == self.caller:
                self.mailbox.store.mark_read(msg.id)
        self.prompt()

    def kill(self, args) -> None:
        msg = self._number(args)
        if msg is not None:
            self.mailbox.store.delete(msg.id)
            self.say(_("Message {n} deleted.").format(n=msg.id))
        self.prompt()

    def send(self, args) -> None:
        mb = self.mailbox
        to = args[0].upper() if args else mb.owner
        if to != mb.owner and not mb.config.allow_third_party:
            self.say(_("This mailbox only takes messages for {owner}.").format(owner=mb.owner))
            self.prompt()
            return
        left = sum(1 for m in mb.store.all() if m.sender == self.caller)
        if left >= mb.config.max_per_caller:
            self.say(_("You have left as many messages as this mailbox keeps from one station."))
            self.prompt()
            return
        self.draft = {"to": to}
        self.state = "subject"
        self.say(_("Subject:"))

    def body_line(self, text: str) -> None:
        if text.strip().upper() == "/ABORT":
            self.state = "command"
            self.say(_("Message abandoned."))
            self.prompt()
            return
        end = text.strip().upper() in _END or text.endswith("\x1a")
        if text.endswith("\x1a"):
            text = text[:-1]
            if text.strip():
                self.draft["body"].append(text)
        elif not end:
            self.draft["body"].append(text)
        size = sum(len(line) + 1 for line in self.draft["body"])
        if size > self.mailbox.config.max_message_bytes:
            self.state = "command"
            self.say(_("That message is too long for this mailbox; it was not kept."))
            self.prompt()
            return
        if not end:
            return
        self.state = "command"
        msg = self.mailbox.store.add(self.caller, self.draft["to"], self.draft["subject"],
                                     "\n".join(self.draft["body"]), self.mailbox.wall_clock())
        self.say(_("Message {n} kept for {to}. Thank you.").format(n=msg.id, to=msg.to))
        self.prompt()
        self.mailbox.engine._emit(Event("mail", "mail-received", self.conn, {"message": msg}))

    def forward(self, sid: fbb.Sid) -> None:
        """The caller is a station's mail client (FORWARDING.md §9)."""
        if not sid.forwards:
            self.say(_("Unknown command. H for help."))
            self.prompt()
            return
        self.state = "forward"
        mb = self.mailbox
        self.exchange = fbb.Exchange(
            "answerer", self.caller, _Mailroom(mb, self.conn), lambda line: self.conn.send((line + "\r").encode("utf-8")),
            self.conn.close, mb.engine.clock, version=mb.config.version, max_bytes=mb.config.max_message_bytes,
            their_sid=sid)
        # Whatever followed the SID in the same frame is the exchange's.
        rest, self.buffer = self.buffer, b""
        if rest:
            self.data(rest)

    def bye(self, args) -> None:
        self.say(_("73, goodbye."))
        self.state = "closing"
        self.conn.close()


class _Mailroom:
    """What the mailbox offers a forwarding caller and takes from it."""

    def __init__(self, mailbox: Mailbox, conn):
        self.mailbox = mailbox
        self.conn = conn

    def outgoing(self, peer: str) -> list[fbb.OutMessage]:
        mb = self.mailbox
        out = []
        for m in mb.store.all():
            if m.to == peer and m.sender == mb.owner and not m.read:
                body = m.body
                p = fbb.Proposal("P", mb.owner, peer, peer, f"{m.id}_{mb.owner}"[:12],
                                 len(body.encode("utf-8")))
                out.append(fbb.OutMessage(p, m.subject, body, key=m.id))
        return out

    def answer(self, peer: str, p: fbb.Proposal) -> str:
        mb = self.mailbox
        to = p.to.split("-")[0].upper()
        if p.type != "P" or mb.store.has_bid(p.bid) or p.size > mb.config.max_message_bytes:
            return "-"
        if to != mb.owner and not mb.config.allow_third_party:
            return "-"
        if sum(1 for m in mb.store.all() if m.sender == peer) >= mb.config.max_per_caller:
            return "="
        return "+"

    def received(self, peer: str, msg: fbb.InMessage) -> None:
        mb = self.mailbox
        p = msg.proposal
        mb.store.remember_bid(p.bid)
        body = msg.body
        # The R: lines BBSs add are routing, not text: kept, but the reader
        # sees the message first.
        kept = mb.store.add(p.sender, p.to.split("-")[0], msg.subject, body, mb.wall_clock())
        mb.engine._emit(Event("mail", "mail-received", self.conn, {"message": kept, "forwarded": True}))

    def delivered(self, peer: str, out: fbb.OutMessage, how: str) -> None:
        self.mailbox.store.mark_read(out.key)


class Mailbox:
    def __init__(self, engine, store: Store | None = None, config: MailboxConfig | None = None,
                 wall_clock: Callable[[], float] = time.time):
        self.engine = engine
        self.store = store or MemoryStore()
        self.config = config or MailboxConfig()
        self.wall_clock = wall_clock
        self.sessions: list[_Session] = []
        self.address = engine.station.address(self.config.ssid)
        engine.listen(self.address, self._accept)
        engine.services.append(self)

    @property
    def owner(self) -> str:
        return self.engine.station.callsign.call

    def _accept(self, call: IncomingCall):
        if not self.config.enabled or not self.config.allow(str(call.remote)):
            return None
        return self._open

    def _open(self, conn) -> None:
        session = _Session(self, conn)
        self.sessions.append(session)
        previous = conn.on_event

        def on_event(event, previous=previous):
            if event.kind == "connected":
                waiting = sum(1 for m in self.store.all() if m.to == session.caller and not m.read)
                session.say(fbb.sid(self.config.version),
                            _("Welcome to the mailbox of {owner}.").format(owner=self.owner),
                            _("Messages waiting for you: {n}. H for help.").format(n=waiting))
                session.prompt()
            if event.kind == "disconnected" and session in self.sessions:
                self.sessions.remove(session)
                if session.exchange is not None:
                    session.exchange.lost()
            if previous is not None:
                previous(event)
        conn.on_event = on_event

    def poll(self, now: float) -> None:
        for session in list(self.sessions):
            if session.exchange is not None:
                session.exchange.poll(now)
                continue
            if session.state != "closing" and now - session.last_input > self.config.idle_timeout_s:
                session.say(_("No input for a while; disconnecting. 73."))
                session.state = "closing"
                session.conn.close()

    def next_deadline(self) -> float | None:
        due = [s.last_input + self.config.idle_timeout_s for s in self.sessions
               if s.state != "closing" and s.exchange is None]
        due += [d for d in (s.exchange.next_deadline() for s in self.sessions if s.exchange is not None) if d]
        return min(due) if due else None
