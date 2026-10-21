"""Finding out what is on the end of a USB cable, and saying what was seen.

The argument for this file is short and was paid for in one afternoon. The
IC-7610 was plugged into `shack` with three things believed about it, and all
three were wrong: the CI-V baud rate was 38400 rather than the 19200 the radio
documents as its default; the radio presents two separate CP2102 bridges rather
than the one two-port CP2105 the design expected; and because there are two
bridges, `ATTRS{interface}` is the identical string on both, so udev cannot say
which of them carries CI-V. Every one of those is a value somebody would
otherwise have typed into a form from a manual, and been wrong.

So the port, the speed and the CI-V address are **discovered, not configured**,
and the discovery is the thing this file is.

**Why it is a registry of strategies rather than a function.** The Raspberry Pi
build in `docs/STANDALONE-PI.md` is an appliance: one radio, switched on at the
wall, in a shack that may be somewhere else entirely, with nobody on site.
Which radio gets plugged into it is not known when it is built - an IC-7600, an
FT-991A and an FT-DX10 have all been named. Stage one drives Icom CI-V and
nothing else, deliberately: a second protocol written against a datasheet with
no radio to check it against would collect the same three corrections as above
with nobody able to find them. What the registry buys is that adding the second
protocol is adding a class here, with the radio in front of you, rather than
unpicking a function that grew an `if`.

**What a strategy may do, and the rule that matters most.** It is asked about
devices it knows nothing about - a GPS, a rotator, a serial console, another
make of radio - because on an appliance those are on the same USB hub. So:
never send anything that could act on an unknown device, treat no answer as the
ordinary case rather than a failure, and be cheap about both. The CI-V query
below is seven bytes with no line terminator in it, which is the deliberate
part: a device that reads lines never sees a complete one and cannot act on it.

**It must be diagnosable from three hundred miles away.** A probe that returns
"no radio found" is useless to somebody who cannot look at the box. So every
port examined, every by-id string, every speed tried, every byte that came back
and the reason each candidate was rejected are kept in a transcript, and the
transcript - not the conclusion - is what goes into the state payload. The most
useful entry in it is not a success or a silence but a *partial* match: a port
that answered something which was not valid CI-V is usually the right port at
the wrong speed, or a radio speaking a protocol this module has not got, and
saying which in words is the difference between a phone call and a site visit.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import civ
from . import models as radio_models
from ..serialio import BAUDS, SerialError, SerialPort

log = logging.getLogger("shack_rig.icom.usbprobe")

#: How long one question is given to be answered. The IC-7610 answers a
#: frequency read in well under a tenth of a second at 38400, measured; this
#: leaves room for a slower radio and for a Pi under load, and it is the number
#: the worst case below is multiplied by, so it is not free.
ASK_SECONDS = 0.3

#: The whole probe gives up after this, whatever is left untried, and says so.
#: An appliance that boots with three unknown USB devices on it must not sit in
#: the dark: it is better to come up saying "I got this far in twenty seconds,
#: here is what I saw" than to be silent for a minute.
DEADLINE_SECONDS = 20.0

#: Where the by-id links live. A probe never holds `/dev/ttyUSB0`: the number
#: is an enumeration order and moves between boots, so a stored one points at
#: the rotator after a reboot. Everything here is the by-id path.
BY_ID = "/dev/serial/by-id"


# -- what was found ---------------------------------------------------------


@dataclass(frozen=True)
class TtyNode:
    """One serial port, as the host presents it before anything is opened."""

    path: str          #: the /dev/serial/by-id/... path, which is what is used
    name: str          #: the by-id basename, which is the evidence in §1
    node: str          #: what it resolves to today, for reading a log by eye

    def public(self) -> dict:
        return {"path": self.path, "name": self.name, "node": self.node}


@dataclass(frozen=True)
class Hint:
    """What a strategy thinks before it has sent a single byte.

    Free evidence, and on an Icom it is decisive: the 7610's by-id string is
    `usb-Silicon_Labs_CP2102_..._IC-7610_23002583_A-if00-port0`, which names
    the model *and* separates the two ports. A generic FTDI cable gives none of
    this, which is why a hint only ever reorders the work rather than deciding
    it.
    """

    model: str | None = None      #: a models.py key
    address: int | None = None    #: the CI-V address that model answers to
    role: str | None = None       #: "control" or "keying", when known
    why: str = ""                 #: in words, for the transcript


@dataclass
class Exchange:
    """One question and whatever came back. The unit of the transcript."""

    baud: int
    sent: bytes
    got: bytes
    verdict: str

    def public(self) -> dict:
        return {"baud": self.baud, "sent": self.sent.hex(" "),
                "got": self.got.hex(" "), "verdict": self.verdict}


@dataclass
class Finding:
    """What the probe concluded about one port, and how it got there."""

    node: TtyNode
    outcome: str                 #: see OUTCOMES
    summary: str = ""            #: one sentence, fit to put on a page
    strategy: str = ""
    model: str | None = None
    address: int | None = None
    baud: int = 0
    exchanges: list[Exchange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "radio"

    def public(self) -> dict:
        return {
            "port": self.node.public(),
            "outcome": self.outcome,
            "summary": self.summary,
            "strategy": self.strategy,
            "model": self.model,
            "address": None if self.address is None else f"0x{self.address:02X}",
            "baud": self.baud or None,
            "exchanges": [e.public() for e in self.exchanges],
        }


#: Every conclusion a port can be given, and what each means to somebody
#: reading it from somewhere else. Written out because these strings reach a
#: page, and a set of outcomes that grows a member nobody named is how a panel
#: ends up rendering a raw enum.
OUTCOMES = {
    "radio": "a radio answered and said what it is",
    "busy": "something else has this port open, so it was left alone",
    "denied": "the port is there and this module may not open it",
    "missing": "the port went away while it was being looked at",
    "foreign": "something answered, but not in a language this module speaks",
    "silent": "the port opened and nothing ever answered on any speed",
    "skipped": "not examined - a radio in this module already holds it",
    "gave-up": "the probe ran out of time before reaching this port",
}


@dataclass
class Report:
    """Everything the probe saw, in the order it saw it."""

    findings: list[Finding] = field(default_factory=list)
    seconds: float = 0.0
    hit_deadline: bool = False
    from_cache: bool = False

    @property
    def chosen(self) -> Finding | None:
        for f in self.findings:
            if f.ok:
                return f
        return None

    def public(self) -> dict:
        got = self.chosen
        return {
            "found": got.public() if got else None,
            "ports": [f.public() for f in self.findings],
            "seconds": round(self.seconds, 2),
            "hit_deadline": self.hit_deadline,
            "from_cache": self.from_cache,
            "outcomes": OUTCOMES,
        }


# -- reading a reply that is not ours ---------------------------------------


def describe_reply(data: bytes) -> str:
    """What something that is not CI-V looks like, in words.

    This is the sentence that saves the day in a fortnight. Bytes on a port
    that are not a frame are *evidence*, and discarding them is how "no radio
    found" gets reported for a radio that is plugged in, switched on and
    talking - at the wrong speed, or in another make's protocol.

    Yaesu CAT is named specifically because two of the three radios that might
    be plugged into an appliance are Yaesu: it is printable ASCII terminated by
    a semicolon, and nothing else on a radio's serial port looks like that. The
    module cannot drive it, and saying so plainly is worth more than silence.
    """
    if not data:
        return "nothing at all"
    text = data.decode("latin-1")
    printable = sum(1 for c in data if 0x20 <= c < 0x7F or c in (0x0A, 0x0D))
    if printable == len(data) and ";" in text:
        return ("printable text ending in a semicolon - this looks like Yaesu "
                "CAT. This module speaks Icom CI-V only, so it cannot drive "
                "this radio, but the port and the speed are right")
    if printable == len(data):
        return f"printable text, not CI-V: {text.strip()!r}"
    if civ.PREAMBLE in data or civ.END in data:
        return ("bytes with CI-V framing in them but no whole frame - very "
                "likely the right port at the wrong speed")
    return ("bytes that are not CI-V and are not text - most often the right "
            "port read at the wrong speed")


# -- the strategies ---------------------------------------------------------


class Strategy:
    """One way of finding out what is on a port. A duck type, not a base class.

    Written down the way `TxAudioSink` is written down in `digi/transmit.py`:
    a documented contract with one implementation, so that the second one is an
    addition rather than an excavation. Three methods:

    `recognise(node)` reads the by-id string and nothing else - no port is
    opened, nothing is sent, and returning None means "no opinion", not "not
    mine". It exists because that string is free and, on an Icom, decisive.

    `bauds(hint)` says which line speeds to try and in what order. Most likely
    first: every speed tried is `ASK_SECONDS` times the number of addresses,
    and on an appliance that is time in the dark.

    `interrogate(port, hint, say, narrow)` asks the open port what it is. It
    must be safe to send to a GPS, a rotator or a serial console; it must call
    `say` with an `Exchange` for everything it sends, whatever came back and
    how it read it, because that transcript is the only thing a person
    somewhere else will have; and it returns a `Finding`-shaped answer or None.
    None means "not what I look for", which is the ordinary case and never an
    error.

    `narrow` is the first of two passes over the speeds, and it is what makes
    the appliance quick. On the first pass a strategy should ask only its single
    best guess; on the second it may try everything it knows. Measured on the
    IC-7610: the radio is on 38400, the search starts at 19200 because that is
    the documented default, and sweeping five CI-V addresses at 19200 before
    reaching 38400 cost 3.7 seconds - to find a radio whose model was already
    written on the USB descriptor. With the narrow pass it is under a second. A
    strategy with nothing to narrow, which a Yaesu one would be, ignores it.

    **What a Yaesu strategy would have to implement.** `recognise` would look
    for the FT-991A's Silicon Labs string and the FT-DX10's FTDI one - neither
    is as helpful as Icom's, which spells the model out. `bauds` would be
    (38400, 19200, 9600, 4800), the FT-DX10's menu default being 38400.
    `interrogate` would send `ID;` and read back `ID0670;` or similar, which
    names the model directly, and then `FA;` for the VFO A frequency as the
    confirming read. That is perhaps forty lines and is *not* the work: driving
    the radio afterwards means a `Link` that is not a CI-V pipe at all and a
    mode vocabulary `models.py` cannot express, which is a design decision and
    not a detail. None of it should be written without the radio present.
    """

    name = "?"

    def recognise(self, node: TtyNode) -> Hint | None:
        raise NotImplementedError

    def bauds(self, hint: Hint | None) -> tuple[int, ...]:
        raise NotImplementedError

    def interrogate(self, port: SerialPort, hint: Hint | None, say,
                    narrow: bool = False) -> Finding | None:
        raise NotImplementedError


class IcomCiv(Strategy):
    """An Icom answering CI-V, which is the one this module can drive."""

    name = "icom-civ"

    #: 19200 is the documented factory default and 38400 is what the one radio
    #: this has been tried against was actually set to, so both are near the
    #: front. 115200 follows because it is the only speed the spectrum scope
    #: fits in (`docs/TRANSPORTS.md` §4.3). The slow ones are last and are
    #: there because an IC-7600 sitting on 9600 from a decade of hamlib is not
    #: a strange thing to find.
    BAUD_ORDER = (19200, 38400, 115200, 9600, 57600, 4800)

    def recognise(self, node: TtyNode) -> Hint | None:
        """Read the by-id string, which on an Icom names the radio.

        `usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_IC-7610_23002583_A-if00-port0`

        Two things come out of that and both were measured rather than assumed.
        The model is in it, which gives the CI-V address to try first. And the
        `_A` / `_B` suffix separates the two bridges, which is the *only* thing
        that does: this radio presents two independent CP2102s, so every udev
        attribute except the serial string is identical on both, and on the one
        radio available `_A` is CI-V and `_B` is silent at every speed tried.

        That last is a convention and not a promise, so it orders the ports
        rather than choosing between them. Both are still tried.
        """
        name = node.name
        want = None
        for key, model in radio_models.MODELS.items():
            # "IC-7610" in the descriptor, "ic-7610" in the table.
            if key.replace("-", "").upper() in name.replace("-", "").upper():
                want = model
                break
        if want is None:
            return None
        role = None
        if "_A-" in name:
            role = "control"
        elif "_B-" in name:
            role = "keying"
        why = f"the port names itself {want.name}"
        if role:
            why += f", and the {name.split('_')[-1].split('-')[0]} suffix is " \
                   f"conventionally the {role} port"
        return Hint(model=want.key, address=want.civ_address, role=role, why=why)

    def bauds(self, hint: Hint | None) -> tuple[int, ...]:
        return self.BAUD_ORDER

    def addresses(self, hint: Hint | None) -> tuple[int, ...]:
        """Which CI-V addresses to ask at, most likely first.

        **The radio will not answer a broadcast**, which is the finding that
        decides the shape of this. CI-V documents 0x00 as "everybody", and
        `0x19 0x00` - read the transceiver's own address - would have made the
        whole sweep unnecessary if the two worked together. Measured on the
        IC-7610: `FE FE 00 E0 19 00 FD` is ignored, three times over, on a link
        that answers the identical question at 0x98 every time. So the address
        is swept, and 0x00 is tried last in case a radio that does honour it
        turns up.

        The sweep is short because it is the distinct factory addresses in
        `models.py` and nothing else: an address changed in a radio's own menu
        to something arbitrary cannot be guessed and is what the profile's
        CI-V address field is for.
        """
        seen: list[int] = []
        if hint is not None and hint.address is not None:
            seen.append(hint.address)
        for model in radio_models.MODELS.values():
            if model.civ_address not in seen:
                seen.append(model.civ_address)
        seen.append(0x00)
        return tuple(seen)

    def interrogate(self, port: SerialPort, hint: Hint | None, say,
                    narrow: bool = False) -> Finding | None:
        """Ask each address who it is, and stop at the first that answers.

        Two questions, in this order, and both are reads:

        `0x19 0x00` asks the transceiver for its own CI-V address. It is the
        identity query - it answers with the address in the body as well as in
        the frame header, so a reply is self-confirming rather than inferred -
        and it is the cheapest possible thing to send to a device that might
        not be a radio, being seven bytes with no terminator.

        `0x03` reads the frequency, and is the fallback for a radio too old for
        `0x19`. Its reply carries a frequency, which is evidence a human can
        sanity-check in the transcript: 7.164437 MHz is a radio on 40 m, and no
        amount of framing luck produces that by accident.
        """
        addresses = self.addresses(hint)
        if narrow:
            # The best guess only, so that every speed gets a quick look before
            # any speed gets an exhaustive one. See `narrow` in the contract.
            addresses = addresses[:1]
        for addr in addresses:
            for command, label in ((bytes([0x19, 0x00]), "who are you"),
                                   (civ.read_freq(), "what frequency")):
                sent = civ.frame(command, to=addr, frm=civ.DEFAULT_CTRL_ADDR)
                got = port.exchange(sent, ASK_SECONDS)
                found = self._read(got, addr)
                say(Exchange(port.baud, sent, got,
                             found[1] if found else
                             (f"{label} at 0x{addr:02X}: " + describe_reply(got))))
                if found:
                    return Finding(
                        node=TtyNode("", "", ""),   # filled in by the caller
                        outcome="radio", strategy=self.name,
                        model=(hint.model if hint else None),
                        address=found[0], baud=port.baud,
                        summary=self._confirm(port, found, say),
                    )
        return None

    def _confirm(self, port: SerialPort, found: tuple[int, str], say) -> str:
        """One more read, so the transcript carries something a person knows.

        `0x19` proves a radio is there but its answer is an address, and an
        address looks the same whether the radio is working or sulking. A
        frequency does not: 7.164437 MHz is a radio on 40 m, and no amount of
        framing luck produces that by accident. It costs one exchange on
        success only - never on a port that is not a radio - and it is what
        somebody reading this from another building will actually recognise.
        """
        addr, summary = found
        sent = civ.frame(civ.read_freq(), to=addr, frm=civ.DEFAULT_CTRL_ADDR)
        got = port.exchange(sent, ASK_SECONDS)
        again = self._read(got, addr)
        say(Exchange(port.baud, sent, got,
                     again[1] if again else
                     "asked what frequency, to have something recognisable in "
                     "here: " + describe_reply(got)))
        if again and "MHz" in again[1]:
            return f"{summary}, and {again[1].split('answered with ')[-1]}"
        return summary

    @staticmethod
    def _read(data: bytes, asked: int) -> tuple[int, str] | None:
        """Pull an answer out of whatever came back, or decide it is not one.

        The address that matters is the one in the *from* field of the reply,
        not the one we asked at. Those differ in the case that makes a probe
        worth having at all: a radio whose address was changed in its menu and
        which was reached by the sweep rather than by the model default.

        Our own frames come back on this wire when the radio's CI-V USB Echo
        Back is on, and an echo is addressed *to* the radio and *from* 0xE0.
        Counting one as an answer would report every port as a radio at every
        speed, so the sender is checked rather than only the framing. That is
        the same rule `Radio._on_civ_data` follows for the same reason.
        """
        frames, _ = civ.split_frames(data)
        for f in frames:
            try:
                to, frm, payload = civ.parse(f)
            except civ.CivError:
                continue
            if frm == civ.DEFAULT_CTRL_ADDR:
                continue                      # our own frame, echoed back
            if to != civ.DEFAULT_CTRL_ADDR:
                continue
            if not payload:
                continue
            if payload[0] == 0x19 and len(payload) >= 3:
                return frm, (f"a radio at CI-V address 0x{frm:02X} named "
                             f"itself (it reports 0x{payload[2]:02X})")
            if payload[0] in (0x00, 0x03) and len(payload) >= 6:
                try:
                    hz = civ.decode_freq(payload[1:6])
                except civ.CivError:
                    continue
                return frm, (f"a radio at CI-V address 0x{frm:02X} answered "
                             f"with {hz / 1_000_000:.6f} MHz")
        return None


#: Cheapest first, and the order is the registry's only rule. Adding a strategy
#: is adding an entry here; nothing else in this file knows how many there are.
STRATEGIES: list[Strategy] = [IcomCiv()]


# -- what is plugged in -----------------------------------------------------


def tty_nodes() -> list[TtyNode]:
    """Every serial port the host can name stably, in a stable order.

    Only `/dev/serial/by-id`. A `/dev/ttyUSB0` is an enumeration order: plug a
    GPS in before the radio next boot and the number that was the radio is the
    GPS, and a module holding the number tunes the wrong thing or, worse, opens
    something that is not a radio at all.
    """
    out = []
    for path in sorted(glob.glob(os.path.join(BY_ID, "*"))):
        try:
            node = os.path.realpath(path)
        except OSError:
            node = ""
        out.append(TtyNode(path=path, name=os.path.basename(path), node=node))
    return out


# -- remembering ------------------------------------------------------------


class Cache:
    """What was found last time, keyed by the port's own name.

    The appliance case: a Pi switched off at the wall every night must not
    spend twenty seconds in the dark every morning to rediscover the radio that
    has not moved. Keyed by the by-id string, which is the radio's own USB
    descriptor including its serial number - so a *different* radio plugged
    into the same socket is a different key and gets a fresh probe, which is
    the property that makes caching safe here.

    A miss costs a probe, which is what would have happened anyway. So this
    never has to be invalidated, and nothing reads it as authority: what it
    holds is a starting point that is confirmed on the wire before it is used.
    """

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            got = json.loads(self.path.read_text("utf-8"))
            if isinstance(got, dict):
                self.data = got
        except (OSError, ValueError) as e:
            log.warning("the probe cache at %s is unreadable (%s); "
                        "it will be written again", self.path, e)

    def get(self, name: str) -> dict | None:
        got = self.data.get(name)
        return got if isinstance(got, dict) else None

    def put(self, finding: Finding) -> None:
        if self.path is None or not finding.ok:
            return
        self.data[finding.node.name] = {
            "strategy": finding.strategy, "model": finding.model,
            "address": finding.address, "baud": finding.baud,
            "when": int(time.time()),
        }
        try:
            self.path.write_text(json.dumps(self.data, indent=2), "utf-8")
        except OSError as e:
            log.warning("could not write the probe cache at %s: %s", self.path, e)


# -- the probe --------------------------------------------------------------


def probe(*, prefer_device: str = "", prefer_baud: int = 0,
          prefer_address: int | None = None, prefer_model: str = "",
          skip: tuple[str, ...] = (), cache: Cache | None = None,
          deadline: float = DEADLINE_SECONDS, stop_at_first: bool = True,
          keep_open: bool = True) -> tuple[Report, SerialPort | None]:
    """Find a radio, and record everything seen on the way.

    Synchronous, and run from a worker thread by everything that calls it -
    see `icomusb.UsbLink.start`. The alternative, an async probe, would either
    block the event loop for as long as it runs or need its own reader task per
    port; the one radio already connected over the LAN goes on being polled
    four times a second either way, and a thread is the version of that which
    cannot be got wrong.

    `prefer_*` are what the profile already holds. They do not restrict the
    search, they reorder it: the stored port is examined first at the stored
    speed and address, so a configuration that is right costs one exchange -
    and a configuration that has gone stale still finds the radio instead of
    reporting it missing.

    `skip` is the ports a live radio in this module already holds. **Never
    probe one of those.** A port open twice gives each opener a random half of
    the bytes, which is the serial version of connecting to the IC-9700 twice
    and is worse, because nothing refuses it and neither side fails.

    `keep_open` hands back the port that answered, still open and still locked,
    so the transport does not have to open it a second time and race whatever
    else is on the host for it.

    `stop_at_first` is a separate decision from that and was briefly confused
    with it, which cost twenty seconds: a radio wants the first thing that
    answers and nothing more, but the "what is plugged in" listing on the page
    wants every port looked at, including the ones behind a radio that has
    already been found. Stopping is the default because a radio is the common
    caller - and because the port immediately after an Icom's control port is
    usually that same Icom's keying port, which is the one port on the host it
    is least useful to spend the deadline on.

    **What the worst case costs.** Per port: speeds x addresses x two questions
    x `ASK_SECONDS`, so 6 x 5 x 2 x 0.3s = 18s, plus about 50ms to open at each
    speed. That is over the `deadline`, which is the point of having one: a
    port that is genuinely silent is abandoned and said to be abandoned rather
    than exhausted. With three unknown devices on an appliance the whole run is
    the deadline, 20 seconds, once - and nothing on the next boot, because the
    cache turns a match into a single exchange of about 30ms.
    """
    started = time.monotonic()
    report = Report()
    nodes = tty_nodes()
    if prefer_device and not any(prefer_device in (n.path, n.node) for n in nodes):
        # A port named outside /dev/serial/by-id: a Mac's /dev/cu.*, a
        # Windows COM5, which have no by-id directory. Examined first, as a
        # stored port always is; never searched for, because there is no
        # stable name to search among.
        nodes.insert(0, TtyNode(path=prefer_device, name=os.path.basename(prefer_device),
                                node=prefer_device))
    ordered = _order_ports(nodes, prefer_device, prefer_model)
    winner: SerialPort | None = None

    for node in ordered:
        if time.monotonic() - started > deadline:
            report.hit_deadline = True
            report.findings.append(Finding(
                node=node, outcome="gave-up",
                summary=f"the probe stopped after {deadline:.0f}s before "
                        f"reaching this port"))
            continue
        if node.path in skip or node.node in skip:
            report.findings.append(Finding(
                node=node, outcome="skipped",
                summary="a radio in this module already has this port open"))
            continue
        finding = _examine(node, report, started, deadline, cache,
                           prefer_device, prefer_baud, prefer_address)
        if finding[0].ok and cache is not None and cache.get(node.name):
            # Said out loud, because "it found the radio in 30ms" and "it
            # searched six speeds" look the same in a report otherwise, and the
            # difference is whether the appliance will be quick after a reboot.
            report.from_cache = True
        report.findings.append(finding[0])
        if finding[1] is not None:
            if keep_open and winner is None:
                winner = finding[1]
            else:
                finding[1].close()
        if stop_at_first and finding[0].ok:
            break

    report.seconds = time.monotonic() - started
    if report.chosen is not None and cache is not None:
        cache.put(report.chosen)
    return report, winner


def _order_ports(nodes: list[TtyNode], prefer_device: str,
                 prefer_model: str) -> list[TtyNode]:
    """Most likely first. Cheap, and it is most of what makes the probe fast.

    The stored port leads, then anything whose by-id string names the model the
    profile says it is, then the conventional control-port suffix, then the
    rest. Ordering rather than filtering: every one of those signals has been
    wrong about this radio once already today.
    """
    def rank(n: TtyNode) -> tuple:
        named = bool(prefer_model) and \
            prefer_model.replace("-", "").upper() in n.name.replace("-", "").upper()
        return (n.path != prefer_device, not named, "_A-" not in n.name, n.name)

    return sorted(nodes, key=rank)


def _examine(node: TtyNode, report: Report, started: float, deadline: float,
             cache: Cache | None, prefer_device: str, prefer_baud: int,
             prefer_address: int | None) -> tuple[Finding, SerialPort | None]:
    """One port, every strategy, every speed, until something answers."""
    hints = [(s, s.recognise(node)) for s in STRATEGIES]
    remembered = cache.get(node.name) if cache is not None else None
    saw_something = ""
    exchanges: list[Exchange] = []

    def say(e: Exchange) -> None:
        exchanges.append(e)

    for strategy, hint in hints:
        if remembered and remembered.get("strategy") == strategy.name:
            # What was found last time, confirmed on the wire before it is
            # trusted. A cache that was believed rather than checked would turn
            # "somebody moved the radio to a different speed" into a module
            # that never works again and says nothing about why.
            hint = Hint(model=remembered.get("model") or (hint.model if hint else None),
                        address=remembered.get("address"),
                        role=hint.role if hint else None,
                        why="remembered from the last probe of this same port")
        order = _baud_order(strategy, hint, prefer_baud,
                            remembered.get("baud") if remembered else 0)
        if hint is not None and hint.address is None and prefer_address is not None:
            hint = Hint(hint.model, prefer_address, hint.role, hint.why)
        elif hint is None and prefer_address is not None:
            hint = Hint(None, prefer_address, None, "the profile's CI-V address")

        # Twice over the speeds: once asking only the best guess, then once
        # asking everything. The worst case is the same work in a different
        # order; the ordinary case - a radio whose model is written on its own
        # USB descriptor - goes from seconds to under one.
        for narrow, baud in [(n, b) for n in (True, False) for b in order]:
            if time.monotonic() - started > deadline:
                report.hit_deadline = True
                return Finding(node=node, outcome="gave-up", exchanges=exchanges,
                               summary=f"the probe stopped after {deadline:.0f}s "
                                       f"with speeds left untried"), None
            port = SerialPort(node.path, baud)
            try:
                port.open()
            except SerialError as e:
                # Not tried again at another speed: a port that will not open
                # will not open faster.
                return Finding(node=node, outcome=e.kind, summary=str(e),
                               exchanges=exchanges), None
            try:
                got = strategy.interrogate(port, hint, say, narrow=narrow)
            except OSError as e:
                port.close()
                return Finding(node=node, outcome="missing", exchanges=exchanges,
                               summary=f"{node.path} went away while it was "
                                       f"being read: {e.strerror or e}"), None
            if got is not None:
                got.node = node
                # The transcript goes on the success too, not only on the
                # failures. A radio that was found on the fourth speed tried
                # says something about the radio - it is not on the speed the
                # profile claims - and that is exactly the sort of thing that
                # is invisible until it matters.
                got.exchanges = exchanges
                if got.model is None and hint is not None:
                    got.model = hint.model
                if hint is not None and hint.why:
                    got.summary = f"{got.summary} ({hint.why})"
                return got, port
            port.close()
            # Anything that came back and was not a frame is the interesting
            # case: keep the best description of it for the summary, because a
            # silence and a wrong-speed rattle read identically in a log and
            # mean entirely different things.
            for e in exchanges:
                if e.got and not saw_something:
                    saw_something = describe_reply(e.got)

    if saw_something:
        return Finding(node=node, outcome="foreign", exchanges=exchanges,
                       summary=f"something is on this port but it is not an "
                               f"Icom: {saw_something}"), None
    return Finding(node=node, outcome="silent", exchanges=exchanges,
                   summary="the port opened and nothing answered at any speed "
                           "or address"), None


def _baud_order(strategy: Strategy, hint: Hint | None, prefer_baud: int,
                remembered_baud: int) -> tuple[int, ...]:
    """The strategy's order, with what is already known moved to the front."""
    order = list(strategy.bauds(hint))
    # Last inserted ends up first, so this puts what was *measured* ahead of
    # what was typed. A stored baud is what somebody believed when they filled
    # the form in; a remembered one is what actually answered on this exact
    # port, and when the two disagree the wire is right.
    for first in (prefer_baud, remembered_baud):
        if first in BAUDS:
            if first in order:
                order.remove(first)
            order.insert(0, first)
    return tuple(order)
