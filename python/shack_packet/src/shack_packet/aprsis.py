"""An APRS iGate: what the station hears on RF goes to APRS-IS, and
messages from APRS-IS for stations heard locally come back out on RF.

    gate = IGate("GM4BRI-10", IGateConfig(tx=True), clock=engine.clock)
    line = gate.rf_to_is(frame)          # a line for the server, or None
    out = gate.is_to_rf(line)            # (information field, via) to transmit, or None

    client = AprsIsClient("GM4BRI-10", passcode("GM4BRI"), on_line=..., filter="m/50")
    client.start(); client.send(line); client.stop()

**Outbound only.** The client connects to `rotate.aprs2.net:14580` (the
Tier 2 rotation; a filtered port) as any APRS client does; nothing here
listens.

**RF to IS** follows the APRS-IS iGate rules (aprs-is.net, "IGate
Design"): only UI frames with PID F0; never one whose path has TCPIP,
TCPXX, NOGATE or RFONLY; never a third-party packet (`}`) or a query
(`?`); the path as heard, then `qAR,<igate>` (the q construct a
bidirectional iGate adds); a packet already gated in the last 30 s is
dropped as a duplicate (digipeated copies of one beacon).

**IS to RF** is deliberately narrow, as the guidance asks: only a
*message* (or its ack) addressed to a station **heard directly on RF in
the last `local_s`** (30 minutes) and **not itself on APRS-IS**, never
from a station that is local on RF (it can hear the addressee itself),
at most `max_per_min` a minute, never the same twice in 30 s. It goes out
as a third-party packet, `}SRC>DEST,TCPIP,<igate>*:payload`, with a short
path (WIDE1-1 by default). Position reports, weather and the rest of the
internet's traffic are never put on the air. Transmitting needs the
app's transmit arming like everything else; `tx` off makes this a
receive-only iGate.

The passcode is the standard one APRS-IS derives from the callsign
(it is not a secret, and not a password: it only says "this is a
licensed amateur's client").
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

SERVER = ("rotate.aprs2.net", 14580)
NO_GATE = {"TCPIP", "TCPXX", "NOGATE", "RFONLY"}
DUPE_S = 30.0


def passcode(call: str) -> int:
    """APRS-IS's passcode for `call` (the SSID is ignored)."""
    base = call.upper().split("-")[0].strip()
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def login_line(call: str, code: int, software: str = "OHS-Packet", version: str = "0.3.0",
               filter: str = "") -> str:
    line = f"user {call.upper()} pass {code} vers {software} {version}"
    return line + (f" filter {filter}" if filter else "")


@dataclass
class IGateConfig:
    #: Messages from APRS-IS out on RF (as above). Off: receive-only.
    tx: bool = False
    tx_via: list[str] = field(default_factory=lambda: ["WIDE1-1"])
    local_s: float = 1800.0
    max_per_min: int = 6


@dataclass
class IsPacket:
    source: str
    destination: str
    path: list[str]
    info: str


def parse_is(line: str) -> IsPacket | None:
    """An APRS-IS line (`SRC>DEST,PATH:info`), or None for a comment
    (`#`) or something that does not parse."""
    if not line or line.startswith("#"):
        return None
    head, sep, info = line.partition(":")
    if not sep or ">" not in head:
        return None
    src, _, rest = head.partition(">")
    parts = rest.split(",")
    return IsPacket(src.strip().upper(), parts[0].strip().upper(), [p.strip().upper() for p in parts[1:]], info)


class IGate:
    def __init__(self, call: str, config: IGateConfig | None = None, *, clock: Callable[[], float] = time.monotonic):
        self.call = call.upper()
        self.config = config or IGateConfig()
        self.clock = clock
        #: Stations heard directly on RF: call -> when.
        self.local: OrderedDict[str, float] = OrderedDict()
        #: Stations seen on APRS-IS as a source: call -> when.
        self.on_is: OrderedDict[str, float] = OrderedDict()
        self._recent: OrderedDict[tuple, float] = OrderedDict()
        self._sent_rf: deque[float] = deque()
        self.stats = {"rf_to_is": 0, "is_to_rf": 0, "dropped": 0}

    def _dupe(self, key: tuple) -> bool:
        now = self.clock()
        while self._recent and next(iter(self._recent.values())) < now - DUPE_S:
            self._recent.popitem(last=False)
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    def heard(self, frame) -> None:
        """Every frame heard on RF: who is local (heard direct)."""
        if not frame.digipeated:
            call = str(frame.source).upper()
            self.local.pop(call, None)
            self.local[call] = self.clock()
            while len(self.local) > 5000:
                self.local.popitem(last=False)

    def rf_to_is(self, frame) -> str | None:
        if not frame.is_ui or frame.pid != 0xF0:
            return None
        try:
            info = bytes(frame.info).decode("latin-1")
        except Exception:                               # noqa: BLE001
            return None
        if not info or info[0] in "}?" or "\r" in info or "\n" in info:
            return None
        path = frame.path
        if any(p.rstrip("*").split("-")[0] in NO_GATE or p.rstrip("*") in NO_GATE for p in path):
            return None
        src = str(frame.source).upper()
        if src == self.call:
            return None
        if self._dupe(("is", src, str(frame.destination), info)):
            self.stats["dropped"] += 1
            return None
        self.stats["rf_to_is"] += 1
        return f"{src}>{frame.destination}" + "".join("," + p for p in path) + f",qAR,{self.call}:{info}"

    def seen_on_is(self, pkt: IsPacket) -> None:
        self.on_is.pop(pkt.source, None)
        self.on_is[pkt.source] = self.clock()
        while len(self.on_is) > 20000:
            self.on_is.popitem(last=False)

    def is_to_rf(self, line: str) -> tuple[str, list[str]] | None:
        """What to transmit for this APRS-IS line, or None."""
        pkt = parse_is(line)
        if pkt is None:
            return None
        self.seen_on_is(pkt)
        if not self.config.tx or not pkt.info.startswith(":") or len(pkt.info) < 11:
            return None
        if any(p in NO_GATE - {"TCPIP"} or p.rstrip("*") == "RFONLY" for p in pkt.path):
            return None
        addressee = pkt.info[1:10].strip().upper()
        now = self.clock()
        heard = self.local.get(addressee)
        if heard is None or now - heard > self.config.local_s:
            return None
        if pkt.source in self.local and now - self.local[pkt.source] <= self.config.local_s:
            return None          # the sender is on RF here: the addressee hears it directly
        seen_is = self.on_is.get(addressee)
        if seen_is is not None and now - seen_is <= self.config.local_s:
            return None          # the addressee is on APRS-IS itself
        if self._dupe(("rf", pkt.source, addressee, pkt.info)):
            self.stats["dropped"] += 1
            return None
        while self._sent_rf and self._sent_rf[0] < now - 60.0:
            self._sent_rf.popleft()
        if len(self._sent_rf) >= self.config.max_per_min:
            self.stats["dropped"] += 1
            return None
        self._sent_rf.append(now)
        self.stats["is_to_rf"] += 1
        return f"}}{pkt.source}>{pkt.destination},TCPIP,{self.call}*:{pkt.info}", list(self.config.tx_via)


class AprsIsClient:
    """A line-oriented connection to an APRS-IS server, on a thread of
    its own, reconnecting with a growing pause (10 s to 5 min). Lines
    from the server go to `on_line`; `state` is idle | connecting |
    connected | verified | failed, `error` the last reason.

    `send` never blocks: it is called from the engine's monitors, under
    the engine's lock, so a line is queued (at most QUEUE_MAX; past that
    the newest are dropped and counted) and a writer thread sends it."""

    QUEUE_MAX = 500

    def __init__(self, call: str, code: int, *, on_line: Callable[[str], None], filter: str = "",
                 server: tuple[str, int] = SERVER, version: str = "0.3.0",
                 connect: Callable[..., socket.socket] = socket.create_connection,
                 on_state: Callable[[str], None] | None = None):
        self.call = call.upper()
        self.code = code
        self.on_line = on_line
        self.filter = filter
        self.server = server
        self.version = version
        self.connect = connect
        self.on_state = on_state
        self.state = "idle"
        self.error = ""
        self.lines_in = 0
        self.lines_out = 0
        self.dropped = 0
        self._out: queue.Queue[str] = queue.Queue(self.QUEUE_MAX)
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _set(self, state: str, error: str = "") -> None:
        self.state, self.error = state, error or self.error
        if self.on_state is not None:
            try:
                self.on_state(state)
            except Exception:                           # noqa: BLE001
                log.exception("aprs-is state observer")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aprs-is", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._set("idle")

    def send(self, line: str) -> bool:
        """Queue a line for the server; False (and nothing queued) unless
        logged in and verified."""
        if self.state != "verified":
            return False
        try:
            self._out.put_nowait(line)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _writer(self, sock, done: threading.Event) -> None:
        while not done.is_set():
            try:
                line = self._out.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                sock.sendall((line + "\r\n").encode("latin-1", "replace"))
                self.lines_out += 1
            except OSError as e:
                self.error = str(e)
                return

    def _run(self) -> None:
        pause = 10.0
        while not self._stop.is_set():
            self._set("connecting")
            done = threading.Event()
            try:
                sock = self.connect(self.server, timeout=20)
                sock.settimeout(120)          # servers send a # keepalive every 20 s
                with self._lock:
                    self._sock = sock
                self._set("connected")
                sock.sendall((login_line(self.call, self.code, version=self.version, filter=self.filter)
                              + "\r\n").encode("ascii"))
                threading.Thread(target=self._writer, args=(sock, done), name="aprs-is-out", daemon=True).start()
                buf = b""
                while not self._stop.is_set():
                    data = sock.recv(4096)
                    if not data:
                        raise OSError("closed by the server")
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("latin-1").rstrip("\r")
                        if line.startswith("# logresp"):
                            if " verified" in line:
                                self._set("verified")
                                pause = 10.0
                            else:
                                self._set("failed", "unverified")
                        self.lines_in += 1
                        try:
                            self.on_line(line)
                        except Exception:                   # noqa: BLE001
                            log.exception("aprs-is line handler")
            except OSError as e:
                if self._stop.is_set():
                    break
                self._set("failed", str(e))
            finally:
                done.set()
                with self._lock:
                    sock, self._sock = self._sock, None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._stop.wait(pause):
                break
            pause = min(pause * 2, 300.0)
