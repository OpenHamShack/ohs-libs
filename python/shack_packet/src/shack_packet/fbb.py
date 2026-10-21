"""FBB forwarding: the store-and-forward exchange between a station and a
BBS, or two BBSs, over one connected-mode session. The wire format is
FORWARDING.md beside this package; this module is that document as code.

    ex = Exchange("caller", peer="GM4BRI", mailroom=mine, send=conn_send_line,
                  close=conn.close, clock=engine.clock)
    for line in lines_from_the_link:
        ex.line(line)
    ex.state      # "done" or "failed" (ex.error says why)

`Exchange` knows nothing of stores, radios or sockets: a `Mailroom` says
what to propose to this peer, how to answer a proposal, and is told what
arrived and what was delivered. The BBS's mailroom is in bbs.py; the
desktop's store-and-forward client brings its own. `call()` runs the
calling side over an engine connection, for that client and for the BBS
forwarding to its partners.

**Delivered means acknowledged by the next turn** (FORWARDING.md §8): FBB
sends no per-message answer, so a message we sent is marked delivered
only when the other side's next turn (a block, FF or FQ) arrives after
it. A link lost in between leaves it undelivered; the BID check on the
next attempt keeps it from arriving twice.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SOFTWARE = "OHS"
FEATURES = "FHM$"
MAX_BLOCK = 5
CTRL_Z = "\x1a"
IDLE_S = 120.0
#: How long a caller waits for the BBS's SID and prompt.
HELLO_S = 60.0
#: How long the side that sent or got FQ waits before closing anyway.
CLOSE_S = 30.0

_BID = re.compile(r"^[A-Z0-9_-]{1,12}$")
_SID = re.compile(r"^\[([^\]]+)\]$")


def sid(version: str) -> str:
    """Our SID line, without its end of line."""
    return f"[{SOFTWARE}-{version}-{FEATURES}]"


@dataclass(frozen=True)
class Sid:
    software: str
    version: str
    features: str

    @property
    def forwards(self) -> bool:
        return "F" in self.features.replace("$", "")


def parse_sid(line: str) -> Sid | None:
    """A SID line, or None. The features are the last `-` field, so a
    dotted or dashed version still parses (FORWARDING.md §3)."""
    m = _SID.match(line.strip())
    if not m:
        return None
    parts = m.group(1).split("-")
    if len(parts) < 2:
        return None
    features = parts[-1].upper()
    if not re.fullmatch(r"[A-Z0-9]*\$?", features):
        return None
    return Sid(parts[0], "-".join(parts[1:-1]), features)


@dataclass(frozen=True)
class Proposal:
    type: str            # P | B | T
    sender: str
    at: str
    to: str
    bid: str
    size: int

    def line(self) -> str:
        return f"FB {self.type} {self.sender} {self.at} {self.to} {self.bid} {self.size}"


def parse_proposal(line: str) -> Proposal | None:
    words = line.split()
    if len(words) < 7 or words[0].upper() != "FB":
        return None
    kind, sender, at, to, bid, size = (w.upper() for w in words[1:7])
    if len(kind) != 1 or not _BID.match(bid) or not size.isdigit():
        return None
    return Proposal(kind, sender, at, to, bid, int(size))


def block_checksum(lines: list[str]) -> str:
    """FBB's checksum for `F> HH`: the two's complement of the sum of every
    byte of the block's lines, each with its CR (FORWARDING.md §5)."""
    total = sum(sum((line + "\r").encode("latin-1", "replace")) for line in lines)
    return f"{(-total) & 0xFF:02X}"


@dataclass
class OutMessage:
    """A message a mailroom offers: `key` is the mailroom's own handle,
    handed back in `delivered`."""
    proposal: Proposal
    subject: str
    body: str
    key: object = None


@dataclass
class InMessage:
    proposal: Proposal
    subject: str
    body: str


class Mailroom(Protocol):
    def outgoing(self, peer: str) -> list[OutMessage]: ...

    def answer(self, peer: str, proposal: Proposal) -> str: ...     # "+", "-" or "="

    def received(self, peer: str, message: InMessage) -> None: ...

    def delivered(self, peer: str, message: OutMessage, how: str) -> None: ...   # how: "sent" | "had-it"


_ANSWERS = {"+": "+", "Y": "+", "-": "-", "N": "-", "R": "-", "=": "=", "L": "="}


@dataclass
class Result:
    sent: int = 0
    received: int = 0
    refused: int = 0
    deferred: int = 0


class Exchange:
    """One side of one forwarding session (FORWARDING.md §4).

    `role` is "caller" (it connected; it waits for the answerer's SID and
    prompt, then sends its own SID and proposes first) or "answerer" (the
    BBS: its SID and prompt are already sent; it waits for the caller's
    SID, which the BBS has usually seen already and passes as `their_sid`).
    `send(line)` sends one line (the caller of this class adds the CR);
    `close()` ends the link once what was sent is acknowledged.
    """

    def __init__(self, role: str, peer: str, mailroom: Mailroom, send: Callable[[str], None],
                 close: Callable[[], None], clock: Callable[[], float], *, version: str = "0.3.0",
                 max_bytes: int = 64_000, their_sid: Sid | None = None,
                 on_done: Callable[[Exchange], None] | None = None):
        if role not in ("caller", "answerer"):
            raise ValueError(role)
        self.role = role
        self.peer = peer.upper()
        self.mailroom = mailroom
        self._send = send
        self._close = close
        self.clock = clock
        self.version = version
        self.max_bytes = max_bytes
        self.their_sid = their_sid
        self.on_done = on_done
        self.result = Result()
        self.error = ""
        self._proposed: set[str] = set()      # BIDs proposed this session: never twice
        self._block: list[Proposal] = []      # theirs, being read
        self._accepted: list[Proposal] = []   # theirs, to receive, in order
        self._offer: list[OutMessage] = []    # ours, waiting for FS
        self._in_flight: list[OutMessage] = []  # ours, sent, waiting for their next turn
        self._current: dict | None = None
        self._got_ff = False
        self.deadline = clock() + (HELLO_S if role == "caller" else IDLE_S)
        if role == "caller":
            self.state = "hello"
        elif their_sid is not None:
            self.state = "their-turn"
        else:
            self.state = "wait-sid"

    # ----------------------------------------------------------------- input
    def line(self, text: str) -> None:
        if self.state in ("done", "failed"):
            return
        self.deadline = self.clock() + IDLE_S
        handler = getattr(self, "_in_" + self.state.replace("-", "_"))
        try:
            handler(text)
        except _ProtocolError as e:
            self._fail(str(e))

    def _in_hello(self, text: str) -> None:
        if self.their_sid is None:
            got = parse_sid(text)
            if got is not None:
                self.their_sid = got
            return
        if text.rstrip().endswith(">"):
            if not self.their_sid.forwards:
                self._send("B")
                self._finish("failed", "the other station does not forward (no F in its SID)")
                self._close()
                return
            self._send(sid(self.version))
            self._my_turn()

    def _in_wait_sid(self, text: str) -> None:
        got = parse_sid(text)
        if got is None:
            if text.strip():
                raise _ProtocolError("expected a SID")
            return
        if not got.forwards:
            raise _ProtocolError("the other station does not forward (no F in its SID)")
        self.their_sid = got
        self.state = "their-turn"

    def _in_their_turn(self, text: str) -> None:
        s = text.strip()
        if not s or s.startswith(";"):
            return
        up = s.upper()
        if up.startswith("FB"):
            p = parse_proposal(s)
            if p is None:
                raise _ProtocolError("a proposal that does not parse")
            if not self._block:
                self._acknowledge()
            self._block.append(p)
            if len(self._block) > MAX_BLOCK:
                raise _ProtocolError("more than five proposals in a block")
            return
        if up.startswith("F>"):
            if not self._block:
                raise _ProtocolError("an empty block")
            check = up[2:].strip()
            if check and check != block_checksum([p.line() for p in self._block]):
                # Our own lines are canonical; theirs may differ in spacing,
                # so a mismatch is logged rather than fatal [guess: BPQ's
                # checksum covers the bytes as it sent them].
                log.info("forwarding with %s: block checksum %s does not match ours", self.peer, check)
            self._answer_block()
            return
        if up == "FF":
            self._acknowledge()
            self._got_ff = True
            self._my_turn()
            return
        if up == "FQ":
            self._acknowledge()
            self._finish("done")
            self._close()
            return
        if up.startswith("***"):
            raise _ProtocolError(f"the other station said {s!r}")
        raise _ProtocolError(f"unexpected {s[:40]!r} where a block, FF or FQ belongs")

    def _in_wait_fs(self, text: str) -> None:
        s = text.strip()
        if not s:
            return
        if not s.upper().startswith("FS"):
            raise _ProtocolError(f"expected FS, got {s[:40]!r}")
        answers = s[2:].strip().upper()
        if len(answers) != len(self._offer):
            raise _ProtocolError("FS does not answer every proposal")
        offer, self._offer = self._offer, []
        for ch, msg in zip(answers, offer):
            a = _ANSWERS.get(ch)
            if a is None:
                raise _ProtocolError(f"FS answer {ch!r} is not + - or =")
            if a == "+":
                self._send(msg.subject)
                for body_line in msg.body.split("\n"):
                    self._send(body_line)
                self._send(CTRL_Z)
                self._in_flight.append(msg)
            elif a == "-":
                self.result.refused += 1
                self.mailroom.delivered(self.peer, msg, "had-it")
            else:
                self.result.deferred += 1
        self.state = "their-turn"

    def _in_receiving(self, text: str) -> None:
        cur = self._current
        if cur is None:
            cur = self._current = {"proposal": self._accepted.pop(0), "subject": text.strip()[:120], "lines": [],
                                   "size": 0}
            return
        end = text.endswith(CTRL_Z) or text.strip() == CTRL_Z
        body = text[:-1] if text.endswith(CTRL_Z) else text
        if body and body.strip() != CTRL_Z:
            cur["lines"].append(body)
            cur["size"] += len(body) + 1
            limit = min(self.max_bytes, 2 * max(cur["proposal"].size, 1024))
            if cur["size"] > limit:
                raise _ProtocolError("a message longer than it was proposed")
        if not end:
            return
        self._current = None
        msg = InMessage(cur["proposal"], cur["subject"], "\n".join(cur["lines"]))
        self.mailroom.received(self.peer, msg)
        self.result.received += 1
        if not self._accepted:
            self._my_turn()

    # ---------------------------------------------------------------- output
    def _acknowledge(self) -> None:
        """Their next turn has arrived: what we sent before it was kept."""
        sent, self._in_flight = self._in_flight, []
        for msg in sent:
            self.result.sent += 1
            self.mailroom.delivered(self.peer, msg, "sent")

    def _answer_block(self) -> None:
        block, self._block = self._block, []
        answers = ""
        for p in block:
            if p.type == "T":
                a = "-"
            else:
                a = _ANSWERS.get(self.mailroom.answer(self.peer, p), "-")
            answers += a
            if a == "+":
                self._accepted.append(p)
        self._send("FS " + answers)
        if self._accepted:
            self.state = "receiving"
        else:
            self._my_turn()

    def _my_turn(self) -> None:
        self._got_ff, got_ff = False, self._got_ff
        offer = [m for m in self.mailroom.outgoing(self.peer) if m.proposal.bid not in self._proposed][:MAX_BLOCK]
        if offer:
            for m in offer:
                self._proposed.add(m.proposal.bid)
                self._send(m.proposal.line())
            self._send("F>")
            self._offer = offer
            self.state = "wait-fs"
            return
        if got_ff:
            self._send("FQ")
            self._finish("done")
            self._close()
            return
        self._send("FF")
        self.state = "their-turn"

    def _fail(self, why: str) -> None:
        log.info("forwarding with %s failed: %s", self.peer, why)
        self._send("*** Protocol error")
        self._finish("failed", why)
        self._close()

    def _finish(self, state: str, error: str = "") -> None:
        self.state = state
        self.error = error
        self._in_flight.clear()
        if self.on_done is not None:
            self.on_done(self)

    # ---------------------------------------------------------------- timers
    def lost(self) -> None:
        """The link went down: whatever was in flight stays undelivered."""
        if self.state not in ("done", "failed"):
            self._finish("failed", "the link was lost")

    def poll(self, now: float) -> None:
        if self.state not in ("done", "failed") and now >= self.deadline:
            self._fail("no answer in time")

    def next_deadline(self) -> float | None:
        return None if self.state in ("done", "failed") else self.deadline


class _ProtocolError(Exception):
    pass


class LineBuffer:
    """Bytes from a connection, cut into lines at CR, LF or CR LF (a CR LF
    pair is one end of line). A Ctrl-Z with no end of line after it also
    ends a line, kept on it, because some software sends `text^Z` and then
    nothing. Lines are decoded UTF-8, else Latin-1 (FORWARDING.md §2)."""

    def __init__(self, max_line: int = 4096):
        self.buffer = bytearray()
        self.max_line = max_line
        self._skip_lf = False      # a CR has just ended a line: an LF now is part of it
        self._after_z = False      # a Ctrl-Z has just ended a line: one end of line now is part of it

    def feed(self, data: bytes) -> list[str]:
        out = []
        for ch in data:
            if self._skip_lf and ch == 0x0A:
                self._skip_lf = False
                continue
            self._skip_lf = False
            if self._after_z:
                self._after_z = False
                if ch in (0x0D, 0x0A):
                    self._skip_lf = ch == 0x0D
                    continue
            if ch in (0x0D, 0x0A):
                out.append(self._text(bytes(self.buffer)))
                self.buffer.clear()
                self._skip_lf = ch == 0x0D
                continue
            self.buffer.append(ch)
            if ch == 0x1A:
                out.append(self._text(bytes(self.buffer)))
                self.buffer.clear()
                self._after_z = True
            elif len(self.buffer) >= self.max_line:
                out.append(self._text(bytes(self.buffer)))
                self.buffer.clear()
        return out

    @staticmethod
    def _text(raw: bytes) -> str:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return "".join(ch for ch in text if ch >= " " or ch in "\t\x1a")


@dataclass
class CallResult:
    state: str                      # done | failed
    error: str = ""
    result: Result = field(default_factory=Result)


def call(engine, remote: str, mailroom: Mailroom, *, via=(), version: str = "0.3.0",
         on_done: Callable[[CallResult], None] | None = None):
    """Connect to `remote` and forward as the caller (FORWARDING.md §4).
    Returns the Exchange; `on_done(CallResult)` is told how it ended,
    including a connection that never came up."""
    conn = engine.connect(remote, via=list(via))
    peer = remote.split("-")[0].upper()
    lines = LineBuffer()
    told = []

    def finished(ex: Exchange) -> None:
        if on_done is not None and not told:
            told.append(True)
            on_done(CallResult(ex.state, ex.error, ex.result))

    ex = Exchange("caller", peer, mailroom, lambda line: conn.send((line + "\r").encode("utf-8")),
                  conn.close, engine.clock, version=version, on_done=finished)

    def data(chunk: bytes) -> None:
        for line in lines.feed(chunk):
            ex.line(line)

    def event(e) -> None:
        if e.kind == "disconnected":
            ex.lost()

    conn.on_data = data
    conn.on_event = event
    # Ended exchanges' timers are cleared here, not by themselves: the
    # engine iterates its services while polling them.
    with engine.lock:
        engine.services[:] = [s for s in engine.services if not (isinstance(s, _ExchangeTimer) and s.ended)]
        engine.services.append(_ExchangeTimer(ex))
    return ex


class _ExchangeTimer:
    """Gives a calling exchange the engine's poll, for its idle time-out."""

    def __init__(self, ex: Exchange):
        self.ex = ex

    @property
    def ended(self) -> bool:
        return self.ex.state in ("done", "failed")

    def poll(self, now: float) -> None:
        self.ex.poll(now)

    def next_deadline(self) -> float | None:
        return self.ex.next_deadline()
