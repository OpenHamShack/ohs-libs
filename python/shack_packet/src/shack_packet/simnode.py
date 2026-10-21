"""A pretend G8BPQ node, with its BBS and its chat conference, for the
simulator and the tests: something to connect to that answers the way a
GB7 node does, so the node browser, the BBS helpers, connect scripts and
conferences can be tried end to end with no radio and no real node.

It is a scripted imitation of what an operator sees, not BPQ: the shapes
of the lines are BPQ's (terminal.py lists them), the behaviour is only as
deep as a client needs to be tested against.

- **Node**: replies start "ALIAS:CALL} ". `?` the commands, `I` info,
  `N`/`NODES` the node table, `R`/`ROUTES` the neighbours, `C call` a
  connection onward (to another pretend node held in `onward`, which
  then answers instead), `BBS`, `CHAT`/`CONF`, `B`/`BYE`/`Q`. Anything
  else is "Invalid command", or echoed back when `echo` is set (the
  app's GB7SIM has always echoed, and its tests read that).
- **BBS**: BPQMail's listing (`L`, `LM`), `R n`, `K n`, `SP/SB/S call`
  with "Enter Title (only):" and "/ex", paging every `page_lines` lines
  with "<A>bort, <CR> Continue..>" (sent with no line end, as BPQ does),
  the prompt "de CALL>", and `B` or `NODE` back to the node.
- **Chat**: a conference with pretend members already in it (`members`);
  the first of them answers what you say, `/w` lists who is here, `/q`
  goes back to the node.

`broadcast()` sends the node's NET/ROM NODES broadcast from its own
engine (the simulator decides when). Everything goes out through that
engine, which is on a simulated channel: nothing here reaches a radio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .netrom import Route, build_nodes


@dataclass
class SimMail:
    number: int
    type: str            # P or B
    to: str
    sender: str
    subject: str
    body: str
    at: str = ""
    read: bool = False
    when: float = 0.0


@dataclass
class SimNodeConfig:
    alias: str
    call: str
    greeting: str = ""
    echo: bool = False
    #: (alias, call, quality) of nodes it advertises and lists.
    nodes: list = field(default_factory=list)
    #: (port, call, quality, count) of its neighbours.
    routes: list = field(default_factory=list)
    members: list = field(default_factory=lambda: ["G4SIM", "G8SIM"])
    page_lines: int = 10
    bulletins: int = 14


def _sample_mail(cfg: SimNodeConfig, now: float) -> list[SimMail]:
    out = []
    for i in range(cfg.bulletins):
        out.append(SimMail(1000 + i, "B", "ALL", "G4SIM" if i % 2 else "G8SIM",
                           f"Simulated bulletin {i + 1}", f"Bulletin {i + 1} from the simulator.\r73",
                           at="GBR", when=now - 3600 * (cfg.bulletins - i)))
    return out


class _Session:
    def __init__(self, node: SimBpqNode, conn):
        self.node = node
        self.conn = conn
        self.caller = conn.remote.call
        self.persona = node.config
        self.mode = "node"
        self.line = bytearray()
        self.pending: list[str] = []       # the rest of a paged listing
        self.draft: dict = {}
        conn.on_data = self.data

    # -- sending --------------------------------------------------------------

    def say(self, *lines: str) -> None:
        self.conn.send(("\r".join(lines) + "\r").encode("latin-1", "replace"))

    def node_say(self, *lines: str) -> None:
        p = self.persona
        self.say(f"{p.alias}:{p.call}}} {lines[0]}", *lines[1:])

    def bbs_prompt(self) -> None:
        self.say(f"de {self.persona.call}>")

    def greet(self) -> None:
        p = self.persona
        self.say(p.greeting or f"{p.alias}:{p.call}}} Welcome to {p.call}, a simulated node. ? for commands.")

    # -- receiving ------------------------------------------------------------

    def data(self, data: bytes) -> None:
        for b in data:
            if b in (13, 10):
                text = self.line.decode("latin-1")
                self.line.clear()
                if b == 10 and not text:
                    continue
                self.command(text)
            elif b == 0x1A:
                self.line.clear()
                if self.mode == "bbs-body":
                    self.command("/ex")
            else:
                self.line.append(b)

    def command(self, text: str) -> None:
        handler = {"node": self.node_command, "bbs": self.bbs_command, "bbs-title": self.bbs_title,
                   "bbs-body": self.bbs_body, "chat": self.chat, "paging": self.paging}[self.mode]
        handler(text)

    # -- the node -------------------------------------------------------------

    def node_command(self, text: str) -> None:
        if not text.strip():
            return
        word, _sep, rest = text.strip().partition(" ")
        verb, rest = word.upper(), rest.strip()
        p = self.persona
        if verb in ("?", "H", "HELP"):
            self.node_say("BBS CHAT CONNECT BYE INFO NODES ROUTES ?")
        elif verb in ("I", "INFO"):
            self.node_say(f"{p.call}: a node inside the simulator. No RF.")
        elif verb in ("N", "NODES"):
            pairs = [f"{a}:{c}".ljust(18) for a, c, _q in p.nodes]
            rows = ["".join(pairs[i:i + 4]).rstrip() for i in range(0, len(pairs), 4)]
            self.node_say("Nodes", *rows)
        elif verb in ("R", "ROUTES"):
            rows = [f"{'>' if i == 0 else ' '} {port} {call:<9} {q} {count}"
                    for i, (port, call, q, count) in enumerate(p.routes)]
            self.node_say("Routes", *rows)
        elif verb in ("C", "CONNECT") and rest:
            target = rest.split()[0].upper()
            other = self.node.onward.get(target) or next(
                (o for o in self.node.onward.values() if o.alias == target), None)
            if other is None:
                self.node_say(f"Failure with {target}")
                return
            self.node_say(f"Connected to {other.alias}:{other.call}")
            self.persona = other
            self.greet()
        elif verb == "BBS":
            self.mode = "bbs"
            self.node_say("Connected to BBS")
            mine = sum(1 for m in self.node.mail if m.to == self.caller and not m.read)
            self.say("[BPQ-6.0.24.1-B1FWIHJM$]",
                     f"Hello {self.caller}. Latest Message is {max(m.number for m in self.node.mail)}, "
                     f"you have {mine} new.")
            self.bbs_prompt()
        elif verb in ("CHAT", "CONF"):
            self.mode = "chat"
            self.node_say("Connected to Chat")
            self.say(f"{self.caller} Welcome to the {p.call} chat conference. /h for help.",
                     "Here: " + ", ".join(self.node.config.members + [self.caller]))
        elif verb in ("B", "BYE", "Q", "QUIT"):
            self.say("73")
            self.conn.close()
        elif p.echo:
            self.say(f"You said: {text.strip()}")
        else:
            self.node_say("Invalid command")

    # -- the BBS --------------------------------------------------------------

    def _list(self, items) -> list[str]:
        rows = []
        for m in sorted(items, key=lambda m: -m.number):
            date = time.strftime("%d-%b", time.gmtime(m.when))
            status = "Y" if m.read else ("$" if m.type == "B" else "N")
            at = f"@{m.at}" if m.at else ""
            rows.append(f"{m.number:<6} {date} {m.type}{status:<2} {len(m.body):>5} {m.to:<7}{at:<8} "
                        f"{m.sender:<7} {m.subject}")
        return rows

    def _page(self, rows: list[str]) -> None:
        n = self.persona.page_lines
        if len(rows) <= n:
            if rows:
                self.say(*rows)
            self.bbs_prompt()
            return
        self.say(*rows[:n])
        self.pending = rows[n:]
        self.mode = "paging"
        self.conn.send(b"<A>bort, <CR> Continue..>")

    def paging(self, text: str) -> None:
        self.mode = "bbs"
        if text.strip().upper().startswith("A"):
            self.pending = []
            self.bbs_prompt()
            return
        rows, self.pending = self.pending, []
        self._page(rows)

    def _find(self, rest: str) -> SimMail | None:
        if not rest.strip().isdigit():
            return None
        n = int(rest.strip())
        return next((m for m in self.node.mail if m.number == n), None)

    def bbs_command(self, text: str) -> None:
        word, _sep, rest = text.strip().partition(" ")
        verb = word.upper()
        mail = self.node.mail
        if not verb:
            self.bbs_prompt()
        elif verb == "L":
            self._page(self._list(mail))
        elif verb == "LM":
            self._page(self._list([m for m in mail if m.to == self.caller or m.sender == self.caller]))
        elif verb == "R":
            m = self._find(rest)
            if m is None:
                self.say(f"Message {rest.strip()} not found")
            else:
                if m.to == self.caller:
                    m.read = True
                when = time.strftime("%d-%b %H:%MZ", time.gmtime(m.when))
                self.say(f"From: {m.sender}", f"To: {m.to}", f"Type/Status: {m.type}N",
                         f"Date/Time: {when}", f"Bid: {m.number}_{self.persona.call}", f"Title: {m.subject}", "",
                         *m.body.split("\r"), f"[End of Message #{m.number} from {m.sender}]")
            self.bbs_prompt()
        elif verb == "K":
            m = self._find(rest)
            if m is None or (m.to != self.caller and m.sender != self.caller):
                self.say(f"Message {rest.strip()} not found or not yours")
            else:
                mail.remove(m)
                self.say(f"Message #{m.number} Killed")
            self.bbs_prompt()
        elif verb in ("S", "SP", "SB") and rest.strip():
            self.draft = {"type": "B" if verb == "SB" else "P", "to": rest.split()[0].upper(), "body": []}
            self.mode = "bbs-title"
            self.say("Enter Title (only):")
        elif verb in ("B", "BYE"):
            self.say("73")
            self.conn.close()
        elif verb == "NODE":
            self.mode = "node"
            self.node_say("Returned to node")
        else:
            self.say("Invalid Command")
            self.bbs_prompt()

    def bbs_title(self, text: str) -> None:
        self.draft["subject"] = text.strip()[:60]
        self.mode = "bbs-body"
        self.say("Enter Message Text. End with /ex or ctrl/z")

    def bbs_body(self, text: str) -> None:
        if text.strip().upper() != "/EX":
            self.draft["body"].append(text)
            return
        number = max(m.number for m in self.node.mail) + 1
        body = "\r".join(self.draft["body"])
        self.node.mail.append(SimMail(number, self.draft["type"], self.draft["to"], self.caller,
                                      self.draft["subject"], body, when=self.node.wall()))
        self.mode = "bbs"
        self.say(f"Message: {number} Bid:  {number}_{self.persona.call} Size: {len(body)}")
        self.bbs_prompt()

    # -- the chat conference ----------------------------------------------------

    def chat(self, text: str) -> None:
        t = text.strip()
        if not t:
            return
        low = t.lower()
        members = self.node.config.members
        if low in ("/q", "/quit", "/b", "/bye"):
            self.mode = "node"
            self.say("*** Left the conference")
            self.node_say("Returned to node")
        elif low in ("/w", "/who"):
            self.say("Here: " + ", ".join(members + [self.caller]))
        elif low in ("/h", "/help", "/?"):
            self.say("/w who is here, /q leave")
        elif low.startswith("/"):
            self.say("Unknown command")
        elif members:
            self.say(f"{members[0]}: Hello {self.caller}, I heard: {t[:40]}")


class SimBpqNode:
    """The node on `engine`, answering on config.call. `onward` holds the
    pretend nodes a `C call` reaches (their configs; they answer through
    this same connection, as a node's onward link would)."""

    def __init__(self, engine, config: SimNodeConfig, *, onward=(), wall=time.time):
        self.engine = engine
        self.config = config
        self.wall = wall
        self.onward = {o.call.upper(): o for o in onward}
        self.mail: list[SimMail] = _sample_mail(config, wall())
        self.sessions: list[_Session] = []
        engine.listen(config.call, self._accept)

    def leave_for(self, call: str, subject: str, body: str, sender: str = "G4SIM") -> SimMail:
        """A personal message waiting for `call` in the BBS."""
        number = max(m.number for m in self.mail) + 1
        m = SimMail(number, "P", call.upper(), sender, subject, body, at=self.config.call, when=self.wall())
        self.mail.append(m)
        return m

    def _accept(self, call):
        return self._open

    def _open(self, conn) -> None:
        session = _Session(self, conn)
        self.sessions.append(session)
        previous = conn.on_event

        def on_event(event, previous=previous):
            # Greet on a reset as well as on the connection. When the
            # caller's first round trip outruns its T1 (a loaded computer
            # is enough: the app's simulator runs at wall-clock speed), its
            # SABM retry crosses our UA, and "SABM while connected" resets
            # the link, which by AX.25 2.2 throws away the I frames still
            # queued - the greeting among them. Greeting only on
            # "connected" left the caller at a silent node now and then
            # (the app's step-5 window test failed that way). A real node
            # treats a reset as a new session and sends its connect text
            # again, so this is also the faithful choice.
            if event.kind in ("connected", "reset"):
                session.mode, session.persona = "node", self.config
                session.line.clear()
                session.greet()
            if event.kind == "disconnected" and session in self.sessions:
                self.sessions.remove(session)
            if previous is not None:
                previous(event)
        conn.on_event = on_event

    def broadcast(self):
        """Send this node's NODES broadcast (on its own simulated engine)."""
        routes = [Route(call, alias, self.config.routes[0][1] if self.config.routes else call, q)
                  for alias, call, q in self.config.nodes]
        frame = build_nodes(self.config.call, self.config.alias, routes)
        self.engine.send_frame(frame)
        return frame
