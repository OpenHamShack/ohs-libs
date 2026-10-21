"""A station's store-and-forward mail client: an outbox that delivers
itself when it can, and collection of the station's own mail from its
home BBS. FBB forwarding throughout (fbb.py, FORWARDING.md §9); the
desktop's Mailbox page is its window.

    outbox = Outbox("~/.local/share/.../outbox.json")
    mail = StationMail(engine, outbox, inbox_store, MailConfig(home="GM4BRI-1"),
                       can_send=lambda: armed)
    mail.compose("G4XYZ@GB7XYZ", "Sked", "2 m at 2000?")
    mail.collect_now()

**Where a message goes** (`routes()`), tried in this order until one
takes it, each attempt one connected session:

1. `CALL@BBS`: a forwarding partner whose routes take that BBS (or the
   BBS itself, when it is listed as a partner), else the home BBS, which
   relays it only if it lists this station as a partner (§6).
2. `CALL` alone: **the addressee's own station** - its mailbox, at
   `CALL-<direct_ssid>` (OpenHamShack's default is -1), which takes mail
   for its operator by FBB forwarding (mailbox.py) - then the home BBS,
   which keeps it for the addressee if they are its user, then partners
   with `*`.

**Queued until a route is available.** Nothing is attempted unless
`can_send()` says so (the app: transmit armed and a callsign), and only
one session runs at a time. A message every route refused or failed is
tried again later (RETRY_S), and after `max_rounds` rounds it is marked
failed. "Delivered" means the other side's next turn arrived (§8).

**Collecting** is a session with the home BBS: our mail for it goes,
then the BBS proposes what it holds for this call, which lands in the
station's mailbox store as if it had been left there.

Events (engine on_event, kind "mail"): "mail-delivered", "mail-failed",
"mail-collected" (detail: count), "mail-session" (detail: peer, state,
error). The app turns the first three into notices.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import fbb
from .engine import Event
from .identity import is_callsign

log = logging.getLogger(__name__)

#: Between rounds of attempts on one message: 10 min, 30 min, 1 h, 2 h, 4 h...
RETRY_S = (600.0, 1800.0, 3600.0, 7200.0, 14400.0)


@dataclass
class Peer:
    """Somewhere to connect: a BBS or a station's mailbox. `routes` are
    the @ labels a partner takes (as bbs.Partner: `GB7XXX`, `#79`, `GBR`,
    `*`)."""
    call: str
    via: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return self.call.upper().split("-")[0]

    def takes(self, at: str) -> bool:
        labels = {x for x in at.upper().split(".") if x}
        routes = {r.upper() for r in self.routes}
        return "*" in routes or bool(labels & routes) or (bool(at) and at.upper().split(".")[0] == self.base)

    @classmethod
    def parse(cls, text: str) -> Peer:
        """`GB7XYZ-1 via MB7NXX routes GB7XYZ,#79` (via and routes optional)."""
        words = text.replace(",", " ").split()
        if not words:
            raise ValueError("empty")
        call, via, routes, into = words[0].upper(), [], [], None
        for w in words[1:]:
            if w.lower() in ("via", "routes"):
                into = via if w.lower() == "via" else routes
                continue
            (into if into is not None else via).append(w.upper())
        if not is_callsign(call.split("-")[0]):
            raise ValueError(call)
        return cls(call, via, routes)


@dataclass
class MailConfig:
    #: The home BBS (an OpenHamShack controller's BBS, or a GB7), where the
    #: station collects its mail; blank for none.
    home: str = ""
    home_via: list[str] = field(default_factory=list)
    partners: list[Peer] = field(default_factory=list)
    #: The SSID other OpenHamShack stations' mailboxes answer on.
    direct_ssid: int = 1
    #: Try the addressee's own station for mail without an @BBS.
    direct: bool = True
    collect_every_s: float = 0.0          # 0: only when asked
    max_rounds: int = 6
    max_bytes: int = 8000
    version: str = "0.3.0"


@dataclass
class OutMail:
    id: int
    bid: str
    to: str                 # call, no SSID
    at: str                 # BBS (with hierarchy) or ""
    subject: str
    body: str
    created: float
    state: str = "queued"   # queued | sending | delivered | failed
    rounds: int = 0
    next_s: float = 0.0     # wall clock: not before
    route: str = ""         # where it went, or was last tried
    error: str = ""         # the last reason, a key
    done_at: float = 0.0
    reply_to: int | None = None
    #: Peers it has been proposed to in an earlier session: a "-" from one
    #: of them means it kept it last time, when the link went before the
    #: next turn (§8); from anyone else "-" means refused.
    tried: list[str] = field(default_factory=list)

    @property
    def address(self) -> str:
        return f"{self.to}@{self.at}" if self.at else self.to


def split_address(text: str) -> tuple[str, str]:
    """`G4XYZ@GB7XYZ.#79.GBR.EURO` to (G4XYZ, GB7XYZ.#79.GBR.EURO); a
    call's SSID is dropped (mail is for the operator). ValueError when it
    is not a callsign."""
    to, _, at = text.strip().upper().partition("@")
    to = to.strip().split("-")[0]
    at = at.strip()
    if not is_callsign(to):
        raise ValueError(to)
    if at and not re.fullmatch(r"[A-Z0-9#.\-]{1,40}", at):
        raise ValueError(at)
    return to, at


class Outbox:
    """Outgoing mail, one JSON file (written whole and renamed, like
    mailbox.FileStore), or in memory with `path` None."""

    def __init__(self, path=None, *, call: str = "", clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.call = call.upper()
        self.items: list[OutMail] = []
        self.next = 1
        if self.path is not None and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = set(OutMail.__dataclass_fields__)
            self.items = [OutMail(**{k: v for k, v in m.items() if k in known}) for m in data.get("mail", [])]
            self.next = int(data.get("next", 1))
            for m in self.items:
                if m.state == "sending":        # the app stopped mid-session: try again
                    m.state = "queued"

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"next": self.next, "mail": [asdict(m) for m in self.items]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, address: str, subject: str, body: str, *, reply_to: int | None = None) -> OutMail:
        to, at = split_address(address)
        n = self.next
        self.next += 1
        # BID: <n>_<CALL>, cut to 12 (FORWARDING.md §5). A start time
        # keeps a reinstalled app (numbering from 1 again) from reusing one.
        stamp = int(self.clock()) % 1000
        bid = f"{n}{stamp:03d}_{self.call or 'OHS'}"[:12]
        m = OutMail(n, bid, to, at, subject.strip()[:80] or "-", body, self.clock(), reply_to=reply_to)
        self.items.append(m)
        self.save()
        return m

    def get(self, ident: int) -> OutMail | None:
        return next((m for m in self.items if m.id == ident), None)

    def delete(self, ident: int) -> bool:
        before = len(self.items)
        self.items = [m for m in self.items if m.id != ident or m.state == "sending"]
        if len(self.items) != before:
            self.save()
        return len(self.items) != before

    def retry(self, ident: int) -> None:
        m = self.get(ident)
        if m is not None and m.state in ("failed", "queued"):
            m.state, m.rounds, m.next_s, m.error = "queued", 0, 0.0, ""
            self.save()

    def waiting(self) -> list[OutMail]:
        return [m for m in self.items if m.state in ("queued", "sending")]


class _Room:
    """The mailroom for one session with one peer (fbb.Mailroom)."""

    def __init__(self, mail: StationMail, peer: Peer, offer: list[OutMail], kind: str):
        self.mail = mail
        self.peer = peer
        self.offer = offer
        self.kind = kind              # "direct" | "bbs"
        self.got = 0
        self.earlier = {m.id: list(m.tried) for m in offer}

    def outgoing(self, peer: str) -> list[fbb.OutMessage]:
        out = []
        for m in self.offer:
            if m.state != "sending":
                continue
            body = m.body
            at = m.at or (m.to if self.kind == "direct" else self.peer.base)
            p = fbb.Proposal("P", self.mail.owner, at, m.to, m.bid, len(body.encode("utf-8")))
            out.append(fbb.OutMessage(p, m.subject, body, key=m.id))
        return out

    def answer(self, peer: str, p: fbb.Proposal) -> str:
        mail = self.mail
        if p.type != "P" or p.to.split("-")[0].upper() != mail.owner:
            return "-"
        if mail.inbox.has_bid(p.bid) or p.size > mail.config.max_bytes:
            return "-"
        return "+"

    def received(self, peer: str, msg: fbb.InMessage) -> None:
        mail = self.mail
        mail.inbox.remember_bid(msg.proposal.bid)
        kept = mail.inbox.add(msg.proposal.sender, mail.owner, msg.subject, msg.body, mail.wall_clock())
        self.got += 1
        mail.engine._emit(Event("mail", "mail-received", None, {"message": kept, "collected": True,
                                                                 "from_peer": self.peer.base}))

    def delivered(self, peer: str, out: fbb.OutMessage, how: str) -> None:
        m = self.mail.outbox.get(out.key)
        if m is None:
            return
        if how == "had-it" and self.peer.call not in self.earlier.get(m.id, ()):
            m.error = "refused"          # left "sending": the session's end moves it to the next route
            return
        m.state, m.route, m.done_at, m.error = "delivered", self.peer.call, self.mail.wall_clock(), ""
        if how == "had-it":
            m.error = "had-it"
        self.mail.outbox.save()
        self.mail.engine._emit(Event("mail", "mail-delivered", None, {"mail": m, "peer": self.peer.call}))


class StationMail:
    """The engine service that sends the outbox and collects mail."""

    def __init__(self, engine, outbox: Outbox, inbox, config: MailConfig | None = None, *,
                 can_send: Callable[[], bool] = lambda: True, wall_clock: Callable[[], float] = time.time):
        self.engine = engine
        self.outbox = outbox
        self.inbox = inbox
        self.config = config or MailConfig()
        self.can_send = can_send
        self.wall_clock = wall_clock
        self.session: fbb.Exchange | None = None
        self.session_peer: Peer | None = None
        self.last: dict = {}           # the last session: peer, state, error, sent, received, at
        self._attempt: dict[int, int] = {}      # mail id -> next route index this round
        self._collect_wanted = False
        self._next_collect = (engine.clock() + self.config.collect_every_s) if self.config.collect_every_s else None
        engine.services.append(self)

    @property
    def owner(self) -> str:
        return self.engine.station.callsign.call

    # ------------------------------------------------------------------ commands
    def compose(self, address: str, subject: str, body: str, *, reply_to: int | None = None) -> OutMail:
        with self.engine.lock:
            m = self.outbox.add(address, subject, body, reply_to=reply_to)
        self.engine._wake()
        return m

    def collect_now(self) -> bool:
        """Collect from the home BBS as soon as a session can run. False
        when there is no home BBS."""
        if not self.config.home:
            return False
        with self.engine.lock:
            self._collect_wanted = True
        self.engine._wake()
        return True

    def send_now(self) -> None:
        """Try everything queued now, not at its retry time."""
        with self.engine.lock:
            for m in self.outbox.waiting():
                m.next_s = 0.0
        self.engine._wake()

    @property
    def busy(self) -> bool:
        return self.session is not None and self.session.state not in ("done", "failed")

    # ------------------------------------------------------------------ routing
    def home(self) -> Peer | None:
        return Peer(self.config.home.upper(), list(self.config.home_via)) if self.config.home else None

    def routes(self, m: OutMail) -> list[tuple[str, Peer]]:
        """Where `m` may go, in order: ("direct" | "bbs", peer)."""
        out: list[tuple[str, Peer]] = []
        home = self.home()

        def add(kind, peer):
            if peer is not None and all(p.call != peer.call for _, p in out):
                out.append((kind, peer))
        if m.at:
            for p in self.config.partners:
                if p.routes and p.takes(m.at) and "*" not in {r.upper() for r in p.routes}:
                    add("bbs", p)
            for p in self.config.partners:
                if not p.routes and p.base == m.at.split(".")[0]:
                    add("bbs", p)
            if home is not None:
                add("bbs", home)
            for p in self.config.partners:
                if "*" in {r.upper() for r in p.routes}:
                    add("bbs", p)
            return out
        if self.config.direct and m.to != self.owner:
            ssid = self.config.direct_ssid
            add("direct", Peer(f"{m.to}-{ssid}" if ssid else m.to))
        if home is not None:
            add("bbs", home)
        for p in self.config.partners:
            if "*" in {r.upper() for r in p.routes}:
                add("bbs", p)
        return out

    # ------------------------------------------------------------------ the engine's clock
    def poll(self, now: float) -> None:
        if self.busy:
            return
        if self._next_collect is not None and now >= self._next_collect:
            self._next_collect = now + self.config.collect_every_s
            self._collect_wanted = True
        if not self.can_send():
            return
        if self._collect_wanted and self.config.home:
            self._collect_wanted = False
            home = self.home()
            offer = [m for m in self.outbox.waiting() if m.state == "queued"
                     and any(p.call == home.call for _, p in self.routes(m))]
            self._start("bbs", home, offer, collecting=True)
            return
        wall = self.wall_clock()
        for m in self.outbox.waiting():
            if m.state != "queued" or m.next_s > wall:
                continue
            routes = self.routes(m)
            at = self._attempt.get(m.id, 0)
            if not routes or at >= len(routes):
                self._round_over(m, "no-route" if not routes else (m.error or "no-answer"))
                continue
            kind, peer = routes[at]
            # Everything else queued that this peer would take goes too.
            offer = [x for x in self.outbox.waiting() if x.state == "queued" and x.next_s <= wall
                     and self._attempt.get(x.id, 0) < len(self.routes(x))
                     and self.routes(x)[self._attempt.get(x.id, 0)][1].call == peer.call]
            self._start(kind, peer, offer)
            return

    def _start(self, kind: str, peer: Peer, offer: list[OutMail], collecting: bool = False) -> None:
        room = _Room(self, peer, offer, kind)
        for m in offer:
            m.state, m.route, m.error = "sending", peer.call, ""
            if peer.call not in m.tried:
                m.tried.append(peer.call)
        self.outbox.save()
        self.session_peer = peer
        self.last = {"peer": peer.call, "state": "calling", "error": "", "sent": 0, "received": 0,
                     "at": self.wall_clock(), "collecting": collecting}
        self._emit("mail-session", peer=peer.call, state="calling", error="")

        def done(r: fbb.CallResult, offer=offer, peer=peer, room=room) -> None:
            self._done(r, offer, peer, room)
        try:
            self.session = fbb.call(self.engine, peer.call, room, via=peer.via, version=self.config.version,
                                    on_done=done)
        except Exception as e:                      # noqa: BLE001 - a refused connect (return path down)
            log.info("mail: could not call %s: %s", peer.call, e)
            self._done(fbb.CallResult("failed", "no-connect"), offer, peer, room)

    def _done(self, r: fbb.CallResult, offer: list[OutMail], peer: Peer, room: _Room) -> None:
        reason = "" if r.state == "done" else _reason(r.error)
        if r.state != "done" and self.session is not None and self.session.their_sid is None:
            reason = "no-answer"           # never connected, or nothing that speaks FBB answered
        self.last.update(state=r.state, error=reason, sent=r.result.sent, received=room.got)
        for m in offer:
            if m.state != "sending":
                continue
            m.state = "queued"
            m.error = m.error or reason or "not-taken"
            self._attempt[m.id] = self._attempt.get(m.id, 0) + 1
            if self._attempt[m.id] >= len(self.routes(m)):
                self._round_over(m, m.error)
        self.outbox.save()
        if room.got:
            self._emit("mail-collected", count=room.got, peer=peer.call)
        self._emit("mail-session", peer=peer.call, state=r.state, error=reason)
        self.engine._wake()

    def _round_over(self, m: OutMail, reason: str) -> None:
        self._attempt.pop(m.id, None)
        m.rounds += 1
        m.error = reason
        if m.rounds >= self.config.max_rounds:
            m.state, m.done_at = "failed", self.wall_clock()
            self._emit("mail-failed", mail=m, why=reason)
        else:
            m.next_s = self.wall_clock() + RETRY_S[min(m.rounds - 1, len(RETRY_S) - 1)]
        self.outbox.save()

    def _emit(self, key: str, **detail) -> None:
        self.engine._emit(Event("mail", key, None, detail))

    def next_deadline(self) -> float | None:
        if self.busy:
            return None
        due = []
        if self._next_collect is not None:
            due.append(self._next_collect)
        if self._collect_wanted or any(m.state == "queued" for m in self.outbox.items):
            # Retry times are wall-clock; look again every half minute
            # rather than converting between the two clocks.
            due.append(self.engine.clock() + 30.0)
        return min(due) if due else None


def _reason(error: str) -> str:
    """A CallResult's English error as a key the app translates."""
    e = (error or "").lower()
    if "does not forward" in e:
        return "not-a-bbs"
    if "lost" in e:
        return "no-answer"
    if "no answer in time" in e:
        return "timeout"
    if "no-connect" in e:
        return "no-connect"
    return "protocol" if e else ""
