"""Reading what nodes and BBSs print: the prompts, the paging questions,
the NODES and ROUTES tables, a BBS's message list, and who comes and goes
in a conference.

Nothing here sends anything. An app shows the raw text as it arrives and
asks these functions what it was, so a helper that reads it wrong costs a
missing row in a list, never a mangled terminal; every parser returns
what it could read and skips the rest, because the text comes from
strangers' software, configured by strangers.

The shapes, as UK nodes print them (G8BPQ/LinBPQ is most of GB7, then
TheNet/NET/ROM and XRouter; FBB and BPQMail are the BBSs):

- A node's replies start "ALIAS:CALL} ": `EDIN:GB7EDN} Nodes`. The header
  line names the table that follows; NODES lists `ALIAS:CALL` pairs
  several to a line (an alias may be blank: `:GB7XX`); ROUTES lists one
  neighbour a line, `> 1 GB7AB-1   192 5`: active marker, port, call,
  quality, how many nodes it gave us, and `!` when the quality is locked.
- A BBS prompt ends in `>` at the end of the text, with no line end after
  it: `de GB7EDN>` (BPQMail), `GM4BRI de GB7EDN>` and `(#1) GM4BRI de
  GB7EDN>` (FBB).
- Paging: BPQ stops a long listing with `<A>bort, <CR> Continue..>`,
  FBB with `Continue (Y/N) ?` or `More (Y/N)?`. The answer differs (a
  bare CR, or Y), which is why `paging_reply` exists.
- The message list (`L`, `LM`, `LL n`): BPQMail writes `22413 20-Oct PN
  342 GM4BRI @GB7EDN G0ABC Hello`, FBB `12345 PN 1234 GM4BRI @GB7EDN G0ABC
  1020/1530 Hello`: number, then date and type in either order, size, to,
  `@` route (absent for local mail), from, and the subject.
- Conferences (BPQ's chat server, the NET/ROM CONV/convers family) announce
  arrivals and departures on lines with `***` and a callsign, and show
  what people say as `CALL: text` or `<CALL>: text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A callsign as it appears in running text: an ITU prefix (one or two
#: characters, at least one a letter, or a digit then a letter: 2E0, 9A),
#: one digit, a suffix of one to four letters, an optional SSID. Checked by
#: the regex alone: identity.is_callsign is stricter than text needs.
CALL_RE = re.compile(r"\b((?:[A-Z]{1,2}|[0-9][A-Z]|[A-Z][0-9])[0-9][A-Z]{1,4}(?:-(?:1[0-5]|[0-9]))?)\b")

_NODE_PREFIX = re.compile(r"^\s*([A-Z0-9#_-]{0,6}):([A-Z0-9]{1,6}(?:-\d{1,2})?)\}\s?(.*)$", re.IGNORECASE)
_PAIR = re.compile(r"(?<![^\s])([A-Z0-9#_-]{0,6}):([A-Z0-9]{1,6}(?:-\d{1,2})?)(?![^\s])", re.IGNORECASE)
_ROUTE = re.compile(r"^\s*(>)?\s*(\d+)\s+([A-Z0-9]{1,6}(?:-\d{1,2})?)\s+(\d{1,3})\s+(\d+)\s*(!)?", re.IGNORECASE)
_BBS_PROMPT = re.compile(r"(?:^|\s)de\s+[A-Z0-9]{1,6}(?:-\d{1,2})?\s*>\s*$|^[A-Z0-9]{1,6}(?:-\d{1,2})?\s*>\s*$",
                         re.IGNORECASE)
_PAGING = re.compile(r"(?:<A>bort|\bcontinue\b|\bmore\b).*[?>]\s*$|--\s*more\s*--", re.IGNORECASE)
_DATE = r"\d{1,2}-[A-Za-z]{3}|\d{4}/\d{4}"
_MAIL = re.compile(
    r"^\s*(\d+)\s+"
    rf"(?:(?P<d1>{_DATE})\s+)?"
    r"(?P<flags>[PBTA][A-Z$]{0,3})\s+"
    rf"(?:(?P<d2>{_DATE})\s+)?"
    r"(?P<size>\d+)\s+(?P<to>[A-Z0-9/]+(?:-\d{1,2})?)\s+"
    r"(?:@\s*(?P<at>[A-Z0-9.#-]+)\s+)?"
    r"(?P<sender>[A-Z0-9/]+(?:-\d{1,2})?)"
    rf"(?:\s+(?P<d3>{_DATE}))?"
    r"(?:\s+(?P<subject>.*))?$", re.IGNORECASE)
_JOIN = re.compile(r"\b(joined|signed on|logged on|has entered|entered|arrived|connected)\b", re.IGNORECASE)
_LEAVE = re.compile(r"\b(left|signed off|logged off|has left|disconnected|quit|leaving|gone)\b", re.IGNORECASE)
_SAYS = re.compile(r"^\s*<?([A-Z0-9/]{3,10}(?:-\d{1,2})?)>?\s*:\s(.*)$", re.IGNORECASE)


def callsigns(text: str) -> list[str]:
    """Every callsign-shaped word in `text`, upper case, in order, once each."""
    out: list[str] = []
    for m in CALL_RE.finditer(str(text or "").upper()):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def base_call(call: str) -> str:
    """`call` without its SSID or a portable suffix: what a callbook and a
    log know a station by (G0ABC-9 and G0ABC/P are both G0ABC)."""
    c = str(call or "").strip().upper().split("-", 1)[0]
    parts = [p for p in c.split("/") if p]
    if not parts:
        return ""
    # The longest part with a digit in it is the call (GM/G0ABC/P -> G0ABC).
    with_digit = [p for p in parts if any(ch.isdigit() for ch in p) and len(p) >= 3]
    return max(with_digit, key=len) if with_digit else parts[0]


@dataclass(frozen=True)
class NodeLine:
    alias: str
    call: str
    text: str            # the rest of the line, after "} "


def node_line(line: str) -> NodeLine | None:
    """A node's reply line, "ALIAS:CALL} text", or None."""
    m = _NODE_PREFIX.match(line or "")
    if m is None:
        return None
    return NodeLine(m.group(1).upper(), m.group(2).upper(), m.group(3))


def prompt_kind(text: str) -> str:
    """What a prompt (text the far end sent without a line end) asks:
    "page" (a paging question), "bbs" (a BBS waiting for a command) or ""."""
    t = str(text or "").rstrip("\r\n")
    if not t.strip():
        return ""
    if _PAGING.search(t):
        return "page"
    if _BBS_PROMPT.search(t):
        return "bbs"
    return ""


def paging_reply(prompt: str, go_on: bool = True) -> str:
    """What to type at a paging question to carry on (or stop): Y/N where
    it asks for Y/N, else a bare line end to carry on and A to abort
    (BPQ's "<A>bort, <CR> Continue"). Without the line end, which the
    caller adds."""
    yn = re.search(r"\(?\s*Y\s*/\s*N\s*\)?", prompt or "", re.IGNORECASE)
    if yn:
        return "Y" if go_on else "N"
    return "" if go_on else "A"


@dataclass(frozen=True)
class NodeEntry:
    alias: str
    call: str


@dataclass(frozen=True)
class RouteEntry:
    port: int
    call: str
    quality: int
    nodes: int
    active: bool = False
    locked: bool = False


def parse_node_pairs(line: str) -> list[NodeEntry]:
    """The ALIAS:CALL pairs in one line of a NODES listing."""
    out = []
    for m in _PAIR.finditer(line or ""):
        call = m.group(2).upper()
        if CALL_RE.fullmatch(call) or re.fullmatch(r"[A-Z0-9]{1,6}(?:-\d{1,2})?", call):
            out.append(NodeEntry(m.group(1).upper(), call))
    return out


def parse_route(line: str) -> RouteEntry | None:
    m = _ROUTE.match(line or "")
    if m is None:
        return None
    q = int(m.group(4))
    if q > 255:
        return None
    return RouteEntry(int(m.group(2)), m.group(3).upper(), q, int(m.group(5)), bool(m.group(1)), bool(m.group(6)))


class ListingReader:
    """Reads a node's NODES and ROUTES replies out of a session's lines,
    a line at a time: `feed(line)` returns the entries that line held
    (terminal.NodeEntry or RouteEntry), so a table fills while the listing
    arrives and nothing waits for an end a node never marks. `source` is
    the call of the node that printed it: the neighbour every entry is
    reached through.

    A listing starts at a node header naming it ("EDIN:GB7EDN} Nodes")
    and ends at a blank line after rows, any other node line, a BBS or
    paging prompt, or a line that holds nothing it could read once rows
    have come: anything else the operator does next is not read as a
    route.
    """

    def __init__(self):
        self.table = ""
        self.source = ""
        self._rows = 0

    def feed(self, line: str) -> list:
        nl = node_line(line)
        if nl is not None:
            head = nl.text.strip().rstrip(":").lower()
            if head in ("nodes", "routes"):
                self.table, self.source, self._rows = head, nl.call, 0
            else:
                self.table = ""
            return []
        if not self.table:
            return []
        if not line.strip():
            if self._rows:
                self.table = ""
            return []
        if prompt_kind(line):
            self.table = ""
            return []
        if self.table == "nodes":
            got: list = parse_node_pairs(line)
        else:
            r = parse_route(line)
            got = [r] if r is not None else []
        if not got and self._rows:
            self.table = ""
        self._rows += len(got)
        return got


@dataclass(frozen=True)
class MailItem:
    number: int
    type: str            # P personal, B bulletin, T NTS traffic
    status: str          # N new, Y read, F forwarded, K killed, $ bulletin...
    size: int
    to: str
    at: str              # the @ route, "" for local mail
    sender: str
    date: str            # as the BBS wrote it
    subject: str


def parse_mail_line(line: str) -> MailItem | None:
    m = _MAIL.match(line or "")
    if m is None:
        return None
    flags = m.group("flags").upper()
    date = m.group("d1") or m.group("d2") or m.group("d3") or ""
    return MailItem(int(m.group(1)), flags[0], flags[1:], int(m.group("size")), m.group("to").upper(),
                    (m.group("at") or "").upper(), m.group("sender").upper(), date,
                    (m.group("subject") or "").strip())


def parse_mail_list(lines) -> list[MailItem]:
    """Every message line in a BBS's listing (headers and prompts skipped)."""
    return [item for item in (parse_mail_line(line) for line in lines) if item is not None]


@dataclass(frozen=True)
class ConferenceEvent:
    kind: str            # "join", "leave", "say", or "list" (a /who answer)
    call: str
    text: str = ""


def conference_event(line: str, own: str = "") -> ConferenceEvent | None:
    """What a conference line says about who is there: someone arriving
    or leaving (a "***" announcement naming a callsign) or speaking
    ("CALL: text"). `own` is this station's call, never a participant
    of its own conference list."""
    text = str(line or "")
    own = base_call(own)
    if "***" in text:
        if re.search(r"\bconnected to\b", text, re.IGNORECASE):
            return None             # a node's "*** Connected to GB7XX": the link, not a person
        calls = [c for c in callsigns(text) if base_call(c) != own]
        if calls:
            if _LEAVE.search(text):
                return ConferenceEvent("leave", calls[0])
            if _JOIN.search(text):
                return ConferenceEvent("join", calls[0])
        return None
    m = _SAYS.match(text)
    if m is not None and CALL_RE.fullmatch(m.group(1).upper()) and base_call(m.group(1)) != own:
        return ConferenceEvent("say", m.group(1).upper(), m.group(2))
    return None


_HERE = re.compile(r"^\s*(?:\*\*\*.*?\b(?:\d+\s+)?here|here|users|stations|present|in conference)\s*:\s*(.+?)\.?\s*$",
                   re.IGNORECASE)


def here_line(line: str) -> list[str] | None:
    """The calls in a "who is here" answer, or None: "Here: G4SIM, G8ABC"
    (BPQ's chat), and shack_packet.conference's "*** 3 here: G0ABC G4XYZ"
    and its welcome "*** Welcome to the GM4BRI-4 conference. 2 here: ..."."""
    m = _HERE.match(str(line or ""))
    if m is None:
        return None
    return callsigns(m.group(1))


class Participants:
    """Who is in a conference, from its lines: arrivals and speakers in,
    departures out. A station heard speaking was there whether or not its
    arrival was seen (it may have joined before us)."""

    def __init__(self, own: str = ""):
        self.own = own
        self.calls: list[str] = []

    def feed(self, line: str) -> ConferenceEvent | None:
        here = here_line(line)
        if here is not None:
            # The answer to /who replaces what was pieced together.
            own = base_call(self.own)
            self.calls = [c for c in here if base_call(c) != own]
            return ConferenceEvent("list", "", ", ".join(self.calls))
        ev = conference_event(line, self.own)
        if ev is None:
            return None
        if ev.kind in ("join", "say") and ev.call not in self.calls:
            self.calls.append(ev.call)
        elif ev.kind == "leave" and ev.call in self.calls:
            self.calls.remove(ev.call)
        return ev
