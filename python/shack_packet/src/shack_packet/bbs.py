"""A packet BBS on one of the station's SSIDs: personal mail and bulletins
with FBB/BPQ's commands, the conference room, and FBB forwarding with
OpenHamShack stations and partner BBSs. Bytes in, bytes out, like the
mailbox (mailbox.py): the controller's store app runs it on a radio it
holds through the broker (Packet Radio docs/BBS-DESIGN.md).

    store = BbsStore("/data/bbs.db", home="GM4BRI")
    room = Room("GM4BRI-2", clock=engine.clock)
    bbs = Bbs(engine, store, BbsConfig(ssid=1), room=room)

**Who is who.** The AX.25 source call is the user (no passwords on RF:
nothing may be sent encrypted, so nothing secret can be asked for), and
mail belongs to the call without its SSID. A barred call (the sysop's
page) gets DM, as a station nobody listens for does.

**Commands** (case ignored; a line ends at CR, LF or CR LF):

    L            new messages since your last L      LM   your personal mail
    LL n         the last n messages                 LB   bulletins
    L> CALL      to CALL   L< CALL  from CALL        L@ AREA  at AREA
    R n          read one                            RM   all your unread mail
    K n          kill one of yours                   KM   all your read mail
    S CALL [@ BBS]  /  SP CALL [@ BBS]   send personal mail
    SB TOPIC [@ AREA]                    send a bulletin
                 then a subject, then the text, ended by /EX or Ctrl-Z
                 on a line of its own; /ABORT gives up
    N name   NH bbs   NQ qth     about you        I [CALL]   information
    C / CONF / CHAT    the conference room (/quit comes back here)
    H / HELP / ?       the commands               B / BYE / Q   disconnect

A first line that is a SID (`[...F...$]`) turns the session into FBB
forwarding (fbb.py, FORWARDING.md): an OpenHamShack station delivering
and collecting its mail, or a partner BBS.

**Limits** (BbsConfig): one message's size, messages a call may leave in
a day, messages kept for one call, an idle time-out, sessions at once.

Everything a caller reads goes through gettext (i18n.py).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from . import fbb
from .bbsstore import DAY, BbsStore, Message, at_home, base_call
from .conference import Member, Room
from .engine import IncomingCall
from .i18n import _
from .identity import is_callsign

log = logging.getLogger(__name__)

_END = ("/EX", "\x1a")
_TOPIC_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


@dataclass
class Partner:
    """A BBS this one forwards with. `call` is where to connect (with its
    SSID); `routes` are the @ labels it takes (`GB7XXX`, `#79`, `GBR`,
    `*` for everything not local); bulletins go to every partner that has
    `bulletins` on."""
    call: str
    via: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    bulletins: bool = True

    @property
    def peer(self) -> str:
        return base_call(self.call)

    def takes(self, at: str) -> bool:
        labels = {x for x in at.upper().split(".") if x}
        routes = {r.upper() for r in self.routes}
        return "*" in routes or bool(labels & routes) or at_home(at) == self.peer

    @classmethod
    def parse(cls, text: str) -> Partner:
        """`GB7XXX-1 via MB7ABC routes GB7XXX,#79` or just `GB7XXX`
        (settings carry one line each, so this is the form they use)."""
        words = text.replace(",", " ").split()
        if not words:
            raise ValueError(_("an empty partner"))
        call, via, routes, mode = words[0].upper(), [], [], None
        for w in words[1:]:
            lw = w.lower()
            if lw in ("via", "routes"):
                mode = lw
            elif mode == "via":
                via.append(w.upper())
            elif mode == "routes":
                routes.append(w.upper())
        return cls(call, via, routes or [base_call(call)])


@dataclass
class BbsConfig:
    ssid: int | None = 1
    info: str = ""
    qth: str = ""
    #: The BBS's hierarchical address after its call: "#79.GBR.EURO".
    hierarchy: str = ""
    max_message_bytes: int = 8000
    max_per_day: int = 20
    max_held: int = 100
    idle_timeout_s: float = 600.0
    max_sessions: int = 4
    partners: list[Partner] = field(default_factory=list)
    forward_interval_s: float = 1800.0
    version: str = "0.3.0"
    allow: Callable[[str], bool] = field(default=lambda call: True)


def _fmt_time(t: float, long: bool = False) -> str:
    return time.strftime("%d/%m/%Y %H:%MZ" if long else "%d/%m %H%M", time.gmtime(t))


class BbsMailroom:
    """What the BBS proposes to and accepts from a forwarding peer
    (FORWARDING.md §6 and §9)."""

    def __init__(self, bbs: Bbs):
        self.bbs = bbs

    @property
    def store(self) -> BbsStore:
        return self.bbs.store

    def partner(self, peer: str) -> Partner | None:
        return next((p for p in self.bbs.config.partners if p.peer == base_call(peer)), None)

    def is_local(self, msg_at: str) -> bool:
        return not msg_at or at_home(msg_at) == self.store.home

    def outgoing(self, peer: str) -> list[fbb.OutMessage]:
        peer = base_call(peer)
        partner = self.partner(peer)
        out = []
        for m in self.store.messages(states=("new", "read")):
            if self.store.delivered_to(m.number, peer) or m.origin == f"forward:{peer}" or m.sender == peer:
                continue
            if partner is not None:
                if m.type == "B" and not partner.bulletins:
                    continue
                if m.type == "P" and (self.is_local(m.at) or not partner.takes(m.at)):
                    continue
            elif not (m.type == "P" and m.recipient == peer):
                continue
            out.append(self._offer(m))
        return out

    def _offer(self, m: Message) -> fbb.OutMessage:
        cfg = self.bbs.config
        home = self.store.home + (f".{cfg.hierarchy}" if cfg.hierarchy else "")
        qth = f" [{cfg.qth}]" if cfg.qth else ""
        rline = f"R:{time.strftime('%y%m%d/%H%MZ', time.gmtime(self.bbs.wall_clock()))} {m.number}@{home}{qth} OHS{cfg.version}"
        body = rline + "\n" + m.body
        p = fbb.Proposal(m.type, m.sender, m.at or self.store.home, m.recipient, m.bid, len(body.encode("utf-8")))
        return fbb.OutMessage(p, m.subject, body, key=m.number)

    def answer(self, peer: str, p: fbb.Proposal) -> str:
        peer = base_call(peer)
        cfg = self.bbs.config
        user = self.store.user(peer)
        if (user is not None and user.barred) or self.store.has_bid(p.bid) or p.size > cfg.max_message_bytes:
            return "-"
        if p.type not in ("P", "B"):
            return "-"
        if self.partner(peer) is None:
            # A station that is not a partner delivers personal mail for
            # this BBS only: no bulletins, no relaying through the box.
            if p.type != "P" or not self.is_local(p.at) or not is_callsign(p.to):
                return "-"
            now = self.bbs.wall_clock()
            if self.store.sent_since(peer, now - DAY, any_origin=True) >= cfg.max_per_day:
                return "="
            if self.store.held_for(p.to) >= cfg.max_held:
                return "="
        return "+"

    def received(self, peer: str, msg: fbb.InMessage) -> None:
        p = msg.proposal
        kept = self.store.post(p.type, p.sender, p.to, p.at, msg.subject, msg.body, bid=p.bid,
                               origin=f"forward:{base_call(peer)}", now=self.bbs.wall_clock())
        if kept is not None:
            self.bbs.activity("mail-in", peer, f"{kept.number} {p.type} {p.sender}>{p.to} {msg.subject}")

    def delivered(self, peer: str, out: fbb.OutMessage, how: str) -> None:
        peer = base_call(peer)
        p = out.proposal
        final = p.type == "P" and (p.to == peer or self.partner(peer) is not None)
        self.store.mark_delivered(out.key, peer, how, final=final, now=self.bbs.wall_clock())
        self.bbs.activity("mail-out", peer, f"{out.key} {how}")


class _Session:
    def __init__(self, bbs: Bbs, conn):
        self.bbs = bbs
        self.conn = conn
        self.call = base_call(conn.remote.call)
        self.lines = fbb.LineBuffer()
        self.state = "command"
        self.first = True
        self.draft: dict = {}
        self.exchange: fbb.Exchange | None = None
        self.member: Member | None = None
        self.since = bbs.engine.clock()
        self.last_input = self.since
        conn.on_data = self.data

    # ------------------------------------------------------------- plumbing
    def say(self, *lines: str) -> None:
        self.conn.send(("\r".join(lines) + "\r").encode("utf-8"))

    def prompt(self) -> None:
        self.say(f"de {self.bbs.address}>")

    def data(self, chunk: bytes) -> None:
        self.last_input = self.bbs.engine.clock()
        for line in self.lines.feed(chunk):
            if self.state == "closing":
                return
            self.line(line)

    def line(self, text: str) -> None:
        first, self.first = self.first, False
        if self.state == "forward":
            self.exchange.line(text)
            return
        if self.state == "room":
            self.bbs.room.line(self.member, text.replace("\x1a", ""))
            return
        if self.state == "subject":
            self.draft["subject"] = text.strip()[:80] or _("(no subject)")
            self.state = "body"
            self.draft["body"] = []
            self.say(_("Enter your message. End it with /EX on a line of its own, or /ABORT to give up."))
            return
        if self.state == "body":
            self.body_line(text)
            return
        sid = fbb.parse_sid(text) if first else None
        if sid is not None:
            self.start_forwarding(sid)
            return
        words = text.strip().split()
        if not words:
            self.prompt()
            return
        verb, args = words[0].upper(), words[1:]
        # L> CALL, L< CALL, L@ AREA may be written with or without a space.
        if len(verb) > 2 and verb[:2] in ("L>", "L<", "L@"):
            verb, args = verb[:2], [verb[2:]] + args
        handler = {
            "L": self.list_new, "LIST": self.list_new, "LM": self.list_mine, "LL": self.list_last,
            "LB": self.list_bulletins, "L>": self.list_to, "L<": self.list_from, "L@": self.list_at,
            "R": self.read, "READ": self.read, "RM": self.read_mine,
            "K": self.kill, "KILL": self.kill, "KM": self.kill_mine,
            "S": self.send_personal, "SP": self.send_personal, "SEND": self.send_personal,
            "SB": self.send_bulletin,
            "N": self.set_name, "NH": self.set_home, "NQ": self.set_qth,
            "I": self.info, "INFO": self.info,
            "C": self.conference, "CONF": self.conference, "CHAT": self.conference,
            "H": self.help, "HELP": self.help, "?": self.help,
            "B": self.bye, "BYE": self.bye, "Q": self.bye, "QUIT": self.bye,
        }.get(verb)
        if handler is None:
            self.say(_("Unknown command {verb}. H for help.").format(verb=verb))
            self.prompt()
            return
        handler(args)

    # -------------------------------------------------------------- listing
    def _row(self, m: Message) -> str:
        status = {"new": "N", "read": "Y", "forwarded": "F"}.get(m.state, "?")
        if m.type == "P" and m.recipient == self.call and m.state == "new":
            status = "N"
        at = at_home(m.at) if m.at else ""
        return f"{m.number:<6} {m.type}{status} {m.size:>6} {m.recipient:<6} {at:<7} {m.sender:<6} " \
               f"{_fmt_time(m.created)} {m.subject}"

    def _show(self, msgs: list[Message], empty: str) -> None:
        msgs = [m for m in msgs if self.bbs.store.visible_to(self.call, m)]
        if not msgs:
            self.say(empty)
        else:
            self.say(_("Msg#   TS   Size To     @BBS    From   Date      Subject"))
            for m in reversed(msgs):
                self.say(self._row(m))
        self.prompt()
        return msgs

    def list_new(self, args) -> None:
        user = self.bbs.store.user(self.call)
        since = user.last_listed if user else 0
        msgs = self.bbs.store.messages(since=since)
        shown = self._show(msgs, _("No new messages."))
        if msgs:
            self.bbs.store.update_user(self.call, last_listed=max(m.number for m in msgs))
        return shown

    def list_mine(self, args) -> None:
        self._show(self.bbs.store.messages(type_="P", to=self.call), _("No personal mail for you."))

    def list_last(self, args) -> None:
        n = int(args[0]) if args and args[0].isdigit() else 10
        visible = [m for m in self.bbs.store.messages() if self.bbs.store.visible_to(self.call, m)]
        self._show(visible[-max(1, min(n, 200)):], _("No messages."))

    def list_bulletins(self, args) -> None:
        self._show(self.bbs.store.messages(type_="B"), _("No bulletins."))

    def _need(self, args, what: str) -> str | None:
        if not args:
            self.say(_("Which {what}?").format(what=what))
            self.prompt()
            return None
        return args[0].upper()

    def list_to(self, args) -> None:
        who = self._need(args, _("callsign or topic"))
        if who:
            self._show(self.bbs.store.messages(to=who), _("No messages."))

    def list_from(self, args) -> None:
        who = self._need(args, _("callsign"))
        if who:
            self._show(self.bbs.store.messages(sender=who), _("No messages."))

    def list_at(self, args) -> None:
        where = self._need(args, _("area or BBS"))
        if where:
            self._show(self.bbs.store.messages(at=where), _("No messages."))

    # -------------------------------------------------------------- reading
    def _number(self, args) -> Message | None:
        if len(args) != 1 or not args[0].isdigit():
            self.say(_("Which message? Give its number, as L lists them."))
            return None
        msg = self.bbs.store.get(int(args[0]))
        if msg is None or not self.bbs.store.visible_to(self.call, msg):
            self.say(_("There is no message {n} for you.").format(n=args[0]))
            return None
        return msg

    def _read_out(self, msg: Message) -> None:
        to = msg.recipient + (f" @ {msg.at}" if msg.at else "")
        self.say(_("From: {sender}").format(sender=msg.sender),
                 _("To: {to}").format(to=to),
                 _("Type/Status: {type}").format(type=msg.type),
                 _("Date/Time: {when}").format(when=_fmt_time(msg.created, long=True)),
                 _("Bid: {bid}").format(bid=msg.bid),
                 _("Title: {subject}").format(subject=msg.subject), "", *msg.body.split("\n"),
                 _("[End of Message #{n} from {sender}]").format(n=msg.number, sender=msg.sender))
        if msg.type == "P" and msg.recipient == self.call:
            self.bbs.store.mark_read(msg.number, self.bbs.wall_clock())

    def read(self, args) -> None:
        msg = self._number(args)
        if msg is not None:
            self._read_out(msg)
        self.prompt()

    def read_mine(self, args) -> None:
        unread = [m for m in self.bbs.store.messages(type_="P", to=self.call, states=("new",))]
        if not unread:
            self.say(_("No unread personal mail."))
        for m in unread:
            self._read_out(m)
        self.prompt()

    def kill(self, args) -> None:
        msg = self._number(args)
        if msg is not None:
            mine = msg.sender == self.call or (msg.type == "P" and msg.recipient == self.call)
            if not mine:
                self.say(_("Only its sender can kill message {n}.").format(n=msg.number))
            else:
                self.bbs.store.kill(msg.number)
                self.say(_("Message {n} killed.").format(n=msg.number))
                self.bbs.activity("kill", self.call, str(msg.number))
        self.prompt()

    def kill_mine(self, args) -> None:
        done = [m.number for m in self.bbs.store.messages(type_="P", to=self.call, states=("read",))]
        for n in done:
            self.bbs.store.kill(n)
        self.say(_("{n} read messages killed.").format(n=len(done)))
        self.prompt()

    # -------------------------------------------------------------- sending
    @staticmethod
    def _split_at(args) -> tuple[list[str], str]:
        words = " ".join(args).replace("@", " @ ").split()
        if "@" in words:
            i = words.index("@")
            return words[:i], (words[i + 1].upper() if i + 1 < len(words) else "")
        return words, ""

    def send_personal(self, args) -> None:
        words, at = self._split_at(args)
        to = words[0].upper() if words else ""
        if not to or not is_callsign(base_call(to)):
            self.say(_("Send to whom? S followed by a callsign, e.g. S G0ABC."))
            self.prompt()
            return
        self._start_draft("P", base_call(to), at)

    def send_bulletin(self, args) -> None:
        words, at = self._split_at(args)
        topic = words[0].upper() if words else ""
        if not topic or len(topic) > 6 or not set(topic) <= _TOPIC_CHARS:
            self.say(_("SB needs a topic of up to six letters or figures, e.g. SB ALL @ GBR."))
            self.prompt()
            return
        self._start_draft("B", topic, at or "")

    def _start_draft(self, kind: str, to: str, at: str) -> None:
        bbs, cfg = self.bbs, self.bbs.config
        now = bbs.wall_clock()
        if bbs.store.sent_since(self.call, now - DAY) >= cfg.max_per_day:
            self.say(_("You have sent as many messages today as this BBS takes from one station."))
            self.prompt()
            return
        if kind == "P" and bbs.mailroom.is_local(at) and bbs.store.held_for(to) >= cfg.max_held:
            self.say(_("This BBS is holding as much mail for {to} as it keeps; try later.").format(to=to))
            self.prompt()
            return
        if kind == "P" and not bbs.mailroom.is_local(at) and not any(p.takes(at) for p in cfg.partners):
            self.say(_("This BBS does not forward to {at}; it will be kept here for {to} to collect.").format(
                at=at, to=to))
            at = ""
        self.draft = {"type": kind, "to": to, "at": at or (bbs.store.home if kind == "P" else "")}
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
        size = sum(len(line.encode("utf-8")) + 1 for line in self.draft["body"])
        if size > self.bbs.config.max_message_bytes:
            self.state = "command"
            self.say(_("That message is too long for this BBS; it was not kept."))
            self.prompt()
            return
        if not end:
            return
        self.state = "command"
        d = self.draft
        msg = self.bbs.store.post(d["type"], self.call, d["to"], d["at"], d["subject"], "\n".join(d["body"]),
                                  now=self.bbs.wall_clock())
        if msg is None:
            self.say(_("That message could not be kept."))
        else:
            self.say(_("Message {n} saved, for {to} @ {at}.").format(n=msg.number, to=msg.recipient,
                                                                     at=msg.at or self.bbs.store.home))
            self.bbs.activity("mail", self.call, f"{msg.number} {msg.type} {msg.recipient} {msg.subject}")
            self.bbs.engine._emit(_mail_event(self.conn, msg))
        self.prompt()

    # ----------------------------------------------------------------- you
    def set_name(self, args) -> None:
        if args:
            self.bbs.store.update_user(self.call, name=" ".join(args)[:30])
            self.say(_("Name set: {name}").format(name=" ".join(args)[:30]))
        else:
            self.say(_("N followed by your first name."))
        self.prompt()

    def set_home(self, args) -> None:
        if args:
            self.bbs.store.update_user(self.call, home=args[0].upper()[:40])
            self.say(_("Home BBS set: {home}").format(home=args[0].upper()[:40]))
        else:
            self.say(_("NH followed by your home BBS."))
        self.prompt()

    def set_qth(self, args) -> None:
        if args:
            self.bbs.store.update_user(self.call, qth=" ".join(args)[:40])
            self.say(_("QTH set: {qth}").format(qth=" ".join(args)[:40]))
        else:
            self.say(_("NQ followed by your location."))
        self.prompt()

    def info(self, args) -> None:
        if args:
            u = self.bbs.store.user(args[0])
            if u is None or u.last_seen is None:
                self.say(_("{call} has not connected to this BBS.").format(call=base_call(args[0])))
            else:
                self.say(_("{call}: {name}, home BBS {home}, QTH {qth}, last here {when}").format(
                    call=u.call, name=u.name or "-", home=u.home or "-", qth=u.qth or "-",
                    when=_fmt_time(u.last_seen, long=True)))
        else:
            for line in (self.bbs.config.info or _("An OpenHamShack BBS.")).split("|"):
                self.say(line)
        self.prompt()

    def help(self, args) -> None:
        self.say(_("L - new messages     LM - your mail     LL n - the last n     LB - bulletins"),
                 _("L> CALL, L< CALL, L@ AREA - messages to, from, at"),
                 _("R n - read     RM - read all your new mail"),
                 _("K n - kill     KM - kill all your read mail"),
                 _("S CALL [@ BBS] - send mail     SB TOPIC [@ AREA] - send a bulletin"),
                 _("N name, NH bbs, NQ qth - about you     I [CALL] - information"),
                 _("C - the conference room     B - disconnect"))
        self.prompt()

    # ----------------------------------------------------------- the room
    def conference(self, args) -> None:
        room = self.bbs.room
        if room is None:
            self.say(_("There is no conference on this BBS."))
            self.prompt()
            return
        conn = self.conn
        name = conn.remote.call if conn.remote.ssid == 0 else f"{conn.remote.call}-{conn.remote.ssid}"
        member = Member(name, lambda text: conn.send((text + "\r").encode("utf-8")), self._left_room)
        self.member = member
        self.state = "room"
        if not room.join(member):
            self.state = "command"
            self.member = None
            self.prompt()

    def _left_room(self, reason: str) -> None:
        self.member = None
        if self.state == "room":
            self.state = "command"
            self.say(_("Back at the BBS."))
            self.prompt()

    # ----------------------------------------------------------- forwarding
    def start_forwarding(self, sid: fbb.Sid) -> None:
        if not sid.forwards:
            self.say(_("This BBS forwards only with FBB-style partners."))
            self.prompt()
            return
        self.state = "forward"
        self.bbs.activity("forward", self.call, f"started ({sid.software} {sid.version})")

        def done(ex: fbb.Exchange) -> None:
            r = ex.result
            self.bbs.activity("forward", self.call, f"{ex.state} sent {r.sent} received {r.received}"
                              + (f" ({ex.error})" if ex.error else ""))

        self.exchange = fbb.Exchange(
            "answerer", self.call, self.bbs.mailroom, lambda line: self.conn.send((line + "\r").encode("utf-8")),
            self.conn.close, self.bbs.engine.clock, version=self.bbs.config.version,
            max_bytes=self.bbs.config.max_message_bytes, their_sid=sid, on_done=done)

    def bye(self, args) -> None:
        self.say(_("73, goodbye from {bbs}.").format(bbs=self.bbs.address))
        self.state = "closing"
        self.conn.close()

    # -------------------------------------------------------------- timers
    def deadline(self) -> float | None:
        if self.state == "closing":
            return None
        if self.state == "forward":
            return self.exchange.next_deadline()
        if self.state == "room":
            return None          # the room has its own idle time-out
        return self.last_input + self.bbs.config.idle_timeout_s

    def poll(self, now: float) -> None:
        if self.state == "forward":
            self.exchange.poll(now)
        elif self.state not in ("closing", "room") and now - self.last_input >= self.bbs.config.idle_timeout_s:
            self.say(_("No input for a while; disconnecting. 73."))
            self.state = "closing"
            self.conn.close()

    def reset(self) -> None:
        """The caller re-established the link (a SABM while connected):
        what we had queued is gone. Most often its first SABM's UA was lost
        and the greeting with it, which would leave a forwarding partner
        waiting for a prompt for ever, so the greeting is sent again; mid
        forwarding, the exchange cannot recover and ends."""
        if self.state == "forward":
            self.exchange._fail("the link was reset")
        elif self.first:
            self.bbs._welcome(self, again=True)
        elif self.state == "command":
            self.prompt()

    def ended(self) -> None:
        if self.member is not None and self.bbs.room is not None:
            self.bbs.room.leave(self.member, "disconnected")
            self.member = None
        if self.exchange is not None:
            self.exchange.lost()

    def public(self) -> dict:
        return {"call": self.call, "state": self.state, "since": self.since}


def _mail_event(conn, msg: Message):
    from .engine import Event
    return Event("mail", "mail-received", conn, {"message": msg})


class Bbs:
    def __init__(self, engine, store: BbsStore, config: BbsConfig | None = None, *, room: Room | None = None,
                 wall_clock: Callable[[], float] = time.time):
        self.engine = engine
        self.store = store
        self.config = config or BbsConfig()
        self.room = room
        self.wall_clock = wall_clock
        self.sessions: list[_Session] = []
        self.mailroom = BbsMailroom(self)
        self.addr = engine.station.address(self.config.ssid)
        self.address = str(self.addr) if self.addr.ssid else self.addr.call
        #: Told of what happens, for the sysop page and the log:
        #: (kind, call, text); kinds connect, disconnect, mail, mail-in,
        #: mail-out, kill, forward, refused.
        self.observers: list[Callable[[str, str, str], None]] = []
        self._forwards: dict[str, fbb.Exchange] = {}
        self._next_forward = engine.clock() + 60.0
        self._next_sweep = 0.0
        engine.listen(self.addr, self._accept)
        engine.services.append(self)
        if room is not None and room not in engine.services:
            engine.services.append(room)

    def activity(self, kind: str, call: str, text: str = "") -> None:
        log.info("bbs %s %s %s", kind, call, text)
        for fn in list(self.observers):
            try:
                fn(kind, call, text)
            except Exception:                       # noqa: BLE001
                log.exception("bbs observer failed")

    def _accept(self, call: IncomingCall):
        who = base_call(call.remote.call)
        user = self.store.user(who)
        if (user is not None and user.barred) or not self.config.allow(str(call.remote)):
            self.activity("refused", who, "barred")
            return None
        if len([s for s in self.sessions if s.state != "closing"]) >= self.config.max_sessions:
            self.activity("refused", who, "busy")
            return None
        return self._open

    def _open(self, conn) -> None:
        session = _Session(self, conn)
        self.sessions.append(session)
        previous = conn.on_event

        def on_event(event, previous=previous):
            if event.kind == "connected":
                self._welcome(session)
            elif event.kind == "reset" and session in self.sessions:
                session.reset()
            if event.kind == "disconnected" and session in self.sessions:
                self.sessions.remove(session)
                session.ended()
                self.activity("disconnect", session.call)
            if previous is not None:
                previous(event)
        conn.on_event = on_event

    def _welcome(self, s: _Session, again: bool = False) -> None:
        if again:
            user = self.store.user(s.call)
        else:
            user = self.store.seen(s.call, self.wall_clock())
            self.activity("connect", s.call)
        waiting = len(self.store.messages(type_="P", to=s.call, states=("new",)))
        s.say(fbb.sid(self.config.version))
        hello = _("Hello {name}, welcome to the {bbs} BBS.").format(name=user.name or s.call, bbs=self.store.home)
        s.say(hello, _("You have {n} new messages. H for help.").format(n=waiting))
        s.prompt()

    # ------------------------------------------------------------ forwarding
    def forward_now(self, partner: Partner | None = None) -> list[str]:
        """Call each partner (or one) that has mail waiting; returns the
        partners called. A partner already in an exchange is skipped."""
        called = []
        for p in ([partner] if partner else self.config.partners):
            running = self._forwards.get(p.peer)
            if running is not None and running.state not in ("done", "failed"):
                continue
            if not self.mailroom.outgoing(p.peer):
                continue

            def done(r: fbb.CallResult, peer=p.peer) -> None:
                self.activity("forward", peer, f"{r.state} sent {r.result.sent} received {r.result.received}"
                              + (f" ({r.error})" if r.error else ""))

            self.activity("forward", p.peer, "calling")
            self._forwards[p.peer] = fbb.call(self.engine, p.call, self.mailroom, via=p.via,
                                              version=self.config.version, on_done=done)
            called.append(p.peer)
        return called

    # ---------------------------------------------------------------- timers
    def poll(self, now: float) -> None:
        for s in list(self.sessions):
            s.poll(now)
        if self.config.partners and now >= self._next_forward:
            self._next_forward = now + self.config.forward_interval_s
            self.forward_now()
        if now >= self._next_sweep:
            self._next_sweep = now + 3600.0
            gone = self.store.sweep(self.wall_clock())
            if gone:
                self.activity("sweep", "-", f"{gone} messages expired or killed")

    def next_deadline(self) -> float | None:
        due = [d for d in (s.deadline() for s in self.sessions) if d is not None]
        due.append(self._next_sweep)
        if self.config.partners:
            due.append(self._next_forward)
        return min(due)

    def public(self) -> dict:
        return {"address": self.address, "sessions": [s.public() for s in self.sessions],
                "counts": self.store.counts(),
                "partners": [{"call": p.call, "via": p.via, "routes": p.routes,
                              "state": (self._forwards[p.peer].state if p.peer in self._forwards else "idle")}
                             for p in self.config.partners]}
