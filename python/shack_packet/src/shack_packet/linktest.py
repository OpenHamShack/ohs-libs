"""A link test with a friend: the return path through the OpenHamShack
relay, and the telemetry both stations stream through it.

Built for the first on-air test with GM0OTB (19/10/2026): one RF direction
a run (returnpath.py says why), the receiver's answers by internet, and as
much as either station knows about the link, live at the other end and
logged for later analysis. The relay is tools/packet_relay.py, run on the
OpenHamShack controller box (tailnet only); its protocol is in
LINK-TEST.md beside this package's README, and both ends of it are here
and there.

    client = RelayClient("ohs-box", 30480, session="bri-ray-1", role="rf-receiver",
                         call="GM0OTB-1", peer="GM4BRI-1", env=environment(radio, app="0.2.1"),
                         log_path=data / "linktest" / "bri-ray-1.jsonl")
    client.start()                                 # connects outwards; raises LinkTestError
    path = RelayReturnPath(client, "GM4BRI-1", outbound=True)
    engine.attach_return_path(path)
    tele = Telemetry(engine, client, radio).start()
    ...
    tele.stop(); engine.detach_return_path("GM4BRI-1"); client.close()

**Outwards only.** The desktop apps open no listening socket (family rule):
both stations connect to the relay, which pairs them by session id and
role. Nothing here listens.

**Heartbeat both ways.** A ping a second; the relay answers each with a
pong carrying its own clocks. Nothing from the relay for SILENCE_S, the
relay closing, or the other station leaving (the relay says so, or it went
silent) takes the return path down (returnpath.py: the links drop without
transmitting). Losing the relay is for good: a lost client stays lost,
join again for a new run. The other station leaving is not: this client
stays joined and goes back to "waiting", and is "ready" again when a
station rejoins in that role (19/10/2026: on the GM0OTB test the RF
receiver left to change a setting and the sender was thrown out of the
session with it, so both had to join again for every change).

**Time.** Every message carries this station's `mono` (time.monotonic) and
`wall` (time.time). The pongs give an NTP-style estimate of this clock
against the relay's (offset = relay - local at the midpoint, with the round
trip); the estimate from the lowest round trip of the last minute is sent
as telemetry every CLOCK_EVERY_S and logged, so two stations' times can be
put on one axis afterwards.

**Telemetry** (`Telemetry`), each message `{"t": "tele", "kind": ...}`:

- `env` once at start: app version, OS, Python, the radio (Pluto identity,
  firmware, serial, URI, frequency, mode or ladder, TX gain, crystal
  correction, guard state, transmit set up);
- `rx-frame` per frame heard: via (radio or internet), mode, C/N0 and the
  SNR in the mode's occupied bandwidth, the channel level and the
  receiver's frequency offset at that moment, FCS "ok" (frames that fail
  it never reach the engine: `rx` counts them, bad_fcs), the receiver's
  decode lag, the frame's type, N(S)/N(R), length and monitor line (the
  info field of an I frame is never sent, only its length);
- `tx-over` per transmission, sent once it has played: frames, types,
  duration, mode(s), and the radio's counters over it (tx_late,
  tx_late_ms, tx_gaps, tx_slack_min_ms), TX gain, crystal correction,
  Pluto temperature (read on a context of its own, at most every
  TEMP_EVERY_S), guard state;
- `adapt` per adaptation decision (adapt.RxAdapter.history: the mode
  asked for and why);
- `link` per link event (connected, disconnected and why, return path
  on/off/lost, transmission refused) and `link-stats` each interval per
  connection: state, V(S)/V(A)/V(R), RC, SRT and T1V (the round trip as
  the link measures it), queued, the TX mode, and the counters' changes
  (T1 expiries, retransmissions, REJ/SREJ/RR sent...);
- `rx` each interval: frames decoded and FCS failures by receiver, frames
  by mode, level, own echoes, return-path counters - what the other end's
  live view shows;
- `clock` every CLOCK_EVERY_S.

Every message in and out is also appended to `log_path` (JSONL) at this
end, so a run is on record even if the relay is lost.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import platform
import queue
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import ax25
from .errors import PacketError
from .returnpath import ReturnPath

log = logging.getLogger(__name__)

PROTOCOL = 1
#: The relay's port on the controller box (tools/packet_relay.py), behind
#: `tailscale serve --tcp`.
PORT = 30480
ROLES = ("rf-sender", "rf-receiver")
HEARTBEAT_S = 1.0
#: Seconds without a word from the relay before the return path is lost.
#: Under a fast link's T1, so the sender stops before it would retry.
SILENCE_S = 5.0
CONNECT_TIMEOUT_S = 5.0
CLOCK_EVERY_S = 10.0
TEMP_EVERY_S = 30.0
#: The longest line either end sends or accepts.
MAX_LINE = 64 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")


class LinkTestError(PacketError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def stamp() -> dict:
    return {"mono": round(time.monotonic(), 6), "wall": round(time.time(), 6)}


def valid_session(session: str) -> bool:
    return bool(SESSION_RE.match(session or ""))


def parse_address(text: str, default_port: int = PORT) -> tuple[str, int]:
    """"host", "host:port" or "[v6]:port" to (host, port)."""
    text = (text or "").strip()
    if not text:
        raise LinkTestError("bad-address", "no relay address")
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:
        host, port = text, ""
    try:
        port_n = int(port) if port else default_port
    except ValueError:
        raise LinkTestError("bad-address", f"{text!r}: the port is not a number") from None
    if not host or not 0 < port_n < 65536:
        raise LinkTestError("bad-address", f"{text!r} is not host:port")
    return host, port_n


class RelayClient:
    """One station's connection to the relay (module docstring).

    `state`: "new", "waiting" (joined; the other station is not there),
    "ready" (both there: frames can go), "lost" (why in `lost_why`) or
    "closed". Callbacks, all from the client's own threads and none of
    them may block: `on_frame(bytes)` a frame from the other station,
    `on_tele(dict)` its telemetry, `on_state(state, why)`."""

    def __init__(self, host: str, port: int, *, session: str, role: str, call: str, peer: str,
                 env: dict | None = None, log_path: Path | str | None = None,
                 silence_s: float = SILENCE_S, heartbeat_s: float = HEARTBEAT_S):
        if role not in ROLES:
            raise LinkTestError("bad-role", f"role {role!r} is not one of {ROLES}")
        if not valid_session(session):
            raise LinkTestError("bad-session", "a session id is 3-40 letters, digits, - or _")
        self.host, self.port = host, int(port)
        self.session, self.role, self.call, self.peer = session, role, call, peer
        self.env = dict(env or {})
        self.silence_s = silence_s
        self.heartbeat_s = heartbeat_s
        self.on_frame: Callable[[bytes], None] | None = None
        self.on_tele: Callable[[dict], None] | None = None
        self.on_state: Callable[[str, str], None] | None = None
        self.state = "new"
        self.lost_why = ""
        self.peer_hello: dict | None = None
        self.welcome: dict = {}
        self.counts = collections.Counter()
        self._sock: socket.socket | None = None
        self._out: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._last_in = 0.0
        self._leaving = False
        self._ping_n = 0
        self._pings: dict[int, tuple[float, float]] = {}
        #: Clock estimates: (local mono at receipt, offset_s, rtt_s).
        self._clock: collections.deque = collections.deque(maxlen=120)
        self._log = None
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(path, "a", encoding="utf-8", buffering=1)
            self.log_path = path
        else:
            self.log_path = None

    # -- life ------------------------------------------------------------------------

    def start(self) -> RelayClient:
        """Connect, join and wait for the relay's welcome; raises
        LinkTestError ("unreachable", "refused", ...) and leaves nothing
        running if it cannot."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
        except OSError as e:
            self._closed_log()
            raise LinkTestError("unreachable", f"relay {self.host}:{self.port}: {e}") from None
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        hello = {"t": "hello", "v": PROTOCOL, "session": self.session, "role": self.role, "call": self.call,
                 "peer": self.peer, "env": self.env, **stamp()}
        reader = sock.makefile("rb")
        try:
            self._write(hello)
            line = reader.readline(MAX_LINE + 1)
            msg = json.loads(line) if line else None
        except (OSError, ValueError) as e:
            self._abandon(reader)
            raise LinkTestError("unreachable", f"relay {self.host}:{self.port}: no welcome ({e})") from None
        if not msg or msg.get("t") != "welcome":
            self._abandon(reader)
            why = (msg or {}).get("reason", "no welcome")
            raise LinkTestError("refused", f"the relay refused: {why}")
        self._logged("in", msg)
        self.welcome = msg
        sock.settimeout(None)
        self._last_in = time.monotonic()
        self.peer_hello = msg.get("peer")
        self._set_state("ready" if self.peer_hello else "waiting", "joined")
        for name, target in (("reader", lambda: self._read(reader)), ("writer", self._write_loop),
                             ("heartbeat", self._heartbeat)):
            t = threading.Thread(target=target, name=f"linktest-{name}", daemon=True)
            self._threads.append(t)
            t.start()
        return self

    def close(self, why: str = "closed") -> None:
        """Leave: a bye to the relay, then the socket closed. Not "lost":
        the operator ended it."""
        if self.state in ("closed",):
            return
        # Leaving: the relay closing the socket after our bye is not a loss.
        self._leaving = True
        if self._sock is not None and self.state not in ("lost",):
            self._out.put({"t": "bye", "why": why, **stamp()})
            deadline = time.monotonic() + 1.0
            while not self._out.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
        was = self.state
        self.state = "closed"
        self._stop.set()
        self._out.put(None)
        self._shutdown()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        if was != "closed" and self.on_state is not None:
            self.on_state("closed", why)
        self._closed_log()

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    # -- sending -------------------------------------------------------------------------

    def send(self, msg: dict) -> bool:
        """Queue a message (stamped here); never blocks. False once lost or
        closed."""
        if self.state not in ("waiting", "ready"):
            return False
        msg = {**msg, **stamp()}
        self._out.put(msg)
        return True

    def send_frame(self, frame: bytes) -> bool:
        parsed = ax25.parse(frame)
        self.counts["frames_out"] += 1
        return self.send({"t": "frame", "n": self.counts["frames_out"], "hex": bytes(frame).hex(),
                          "monitor": brief(parsed)})

    def clock(self) -> dict | None:
        """The best estimate of the last minute: the relay's clock minus
        ours, from the ping with the shortest round trip."""
        now = time.monotonic()
        recent = [c for c in self._clock if now - c[0] <= 60.0]
        if not recent:
            return None
        best = min(recent, key=lambda c: c[2])
        return {"offset_s": round(best[1], 6), "rtt_s": round(best[2], 6), "samples": len(recent)}

    # -- threads ---------------------------------------------------------------------

    def _write(self, msg: dict) -> None:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_LINE:
            log.warning("linktest: a %d-byte %s message not sent (over %d)", len(data), msg.get("t"), MAX_LINE)
            return
        with self._lock:
            self._sock.sendall(data)
        self._logged("out", msg)

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            msg = self._out.get()
            if msg is None:
                return
            try:
                self._write(msg)
            except OSError as e:
                if not self._leaving:
                    self._lose("relay-closed", f"writing: {e}")
                return

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            if time.monotonic() - self._last_in > self.silence_s:
                self._lose("relay-silent", f"nothing from the relay for {self.silence_s:.0f} s")
                return
            self._ping_n += 1
            t = stamp()
            self._pings[self._ping_n] = (t["mono"], t["wall"])
            if len(self._pings) > 60:
                for k in sorted(self._pings)[:-60]:
                    del self._pings[k]
            self._out.put({"t": "ping", "n": self._ping_n, **t})

    def _read(self, reader) -> None:
        while not self._stop.is_set():
            try:
                line = reader.readline(MAX_LINE + 1)
            except (OSError, ValueError):
                line = b""
            if not line:
                if not self._stop.is_set() and not self._leaving:
                    self._lose("relay-closed", "the relay closed the connection")
                return
            self._last_in = time.monotonic()
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("linktest: a line from the relay that is not JSON, ignored")
                continue
            self._logged("in", msg)
            try:
                self._handle(msg)
            except Exception:                       # noqa: BLE001 - one bad message never ends the test
                log.exception("linktest: handling %s", msg.get("t"))

    def _handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "pong":
            sent = self._pings.pop(msg.get("n"), None)
            if sent is not None and "relay_wall" in msg:
                mono_now, wall_now = time.monotonic(), time.time()
                rtt = mono_now - sent[0]
                offset = msg["relay_wall"] - (sent[1] + wall_now) / 2.0
                self._clock.append((mono_now, offset, rtt))
        elif t == "frame":
            self.counts["frames_in"] += 1
            try:
                frame = bytes.fromhex(msg.get("hex", ""))
            except ValueError:
                return
            if self.on_frame is not None:
                self.on_frame(frame)
        elif t == "tele":
            self.counts["tele_in"] += 1
            if self.on_tele is not None:
                self.on_tele(msg)
        elif t == "peer":
            if msg.get("state") == "joined":
                self.peer_hello = msg.get("hello") or {}
                if self.state == "waiting":
                    self._set_state("ready", "peer-joined")
            elif msg.get("state") == "left":
                self.peer_hello = None
                why = "peer-silent" if msg.get("why") == "timeout" else "peer-left"
                with self._state_lock:
                    if self.state != "ready":
                        return
                    # Stay joined (module docstring): back to waiting, which
                    # takes RelayReturnPath down, so the engine drops the
                    # link without transmitting exactly as for a lost relay.
                    log.warning("linktest %s: the other station left (%s); waiting for it to rejoin",
                                self.session, msg.get("why", "?"))
                    self._logged("event", {"t": "peer-gone", "why": why, "detail": msg.get("why")})
                    self._set_state("waiting", why)
        elif t == "error":
            log.warning("linktest: the relay says %s", msg.get("reason"))

    # -- state -----------------------------------------------------------------------

    def _set_state(self, state: str, why: str) -> None:
        self.state = state
        log.info("linktest %s: %s (%s)", self.session, state, why)
        if self.on_state is not None:
            self.on_state(state, why)

    def _lose(self, why: str, detail: str) -> None:
        with self._state_lock:
            if self.state in ("lost", "closed"):
                return
            self.lost_why = why
            log.warning("linktest %s: return path lost: %s", self.session, detail)
            self._stop.set()
            self._out.put(None)
            self._shutdown()
            self._set_state("lost", why)
            self._logged("event", {"t": "lost", "why": why, "detail": detail})

    def _shutdown(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            self._sock = sock          # kept for _write's lock users; closed

    def _abandon(self, reader) -> None:
        try:
            reader.close()
        finally:
            self._shutdown()
            self._closed_log()

    def _logged(self, direction: str, msg: dict) -> None:
        if self._log is None:
            return
        try:
            self._log.write(json.dumps({"dir": direction, **stamp(), "msg": msg}, separators=(",", ":")) + "\n")
        except (OSError, ValueError):
            pass

    def _closed_log(self) -> None:
        f, self._log = self._log, None
        if f is not None:
            f.close()


def brief(frame) -> str:
    """A frame for logs and the relay: addresses, type, N(S)/N(R), P/F, and
    the length of any info field - never an I frame's payload."""
    if frame is None:
        return "?"
    c = frame.control
    seq = "".join(f" {k}{v}" for k, v in (("S", c.ns), ("R", c.nr)) if v is not None)
    pf = " P/F" if c.pf else ""
    info = f" {len(frame.info)}B" if frame.info else ""
    head = frame.monitor().split(":", 1)[0]
    return f"{head} {c.type.name}{seq}{pf}{info}"


#: The client's reasons for going from ready back to waiting: the other
#: station left or went silent, and may rejoin.
PEER_GONE = ("peer-left", "peer-silent")


class RelayReturnPath(ReturnPath):
    """returnpath.ReturnPath through a RelayClient: up while both stations
    are joined. Down, and its links dropped, when the other station leaves,
    and up again when one rejoins; lost, for good, when the client is."""

    def __init__(self, client: RelayClient, remote, *, outbound: bool):
        super().__init__(remote, outbound=outbound)
        self.client = client
        prev_frame, prev_state = client.on_frame, client.on_state

        def on_frame(frame):
            if prev_frame is not None:
                prev_frame(frame)
            self._deliver(frame)

        def on_state(state, why):
            if prev_state is not None:
                prev_state(state, why)
            if state == "lost" or (state == "waiting" and why in PEER_GONE):
                self._lost(why)
        client.on_frame = on_frame
        client.on_state = on_state

    @property
    def up(self) -> bool:
        return self.client.state == "ready"

    def send(self, frame: bytes) -> None:
        self.client.send_frame(frame)

    def describe(self) -> dict:
        return {**super().describe(), "relay": f"{self.client.host}:{self.client.port}",
                "session": self.client.session, "role": self.client.role}


# -- telemetry -----------------------------------------------------------------------------

def environment(radio=None, *, app: str = "", extra: dict | None = None) -> dict:
    """What the other end and the log should know about this station."""
    from . import __version__ as lib_version
    env = {"app": app, "shack_packet": lib_version, "os": platform.platform(), "python": sys.version.split()[0],
           "machine": platform.machine()}
    if radio is not None:
        env["radio"] = radio_identity(radio)
    env.update(extra or {})
    return env


def radio_identity(radio) -> dict:
    """The radio link's identity and set-up, from what it already holds
    (no radio I/O): a Pluto's model, firmware and serial from its context's
    attributes."""
    out = {"kind": type(radio).__name__}
    for name in ("uri", "frequency", "mode", "modes", "baud", "tx_gain_db", "rx_gain", "xo_applied", "identity",
                 "transmit", "guard_state", "ifo_hz", "rate_iq", "deviation_hz", "turnaround_s"):
        try:
            value = getattr(radio, name)
        except Exception:                           # noqa: BLE001 - a property that needs an open radio
            continue
        if callable(value):
            continue
        out[name] = list(value) if isinstance(value, tuple) else value
    attrs = getattr(getattr(radio, "ctx", None), "attrs", None) or {}
    for key in ("hw_model", "hw_model_variant", "hw_serial", "fw_version", "ad9361-phy,model", "local,kernel"):
        if key in attrs:
            out[key] = str(attrs[key])
    return _plain(out)


def _plain(value):
    """JSON-safe: numbers that are not finite become None, sets and tuples
    lists, anything else unknown its str()."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


class PlutoTemperature:
    """The AD936x's temperature, read on a context of its own (as the
    link's TX watchdog has), so nothing here shares the link's context or
    its locks. Never writes. None when the radio is not a Pluto, has no
    temp0, or the read fails (it then stops trying)."""

    def __init__(self, radio):
        self.radio = radio
        self._chan = None
        self._failed = False

    def read(self) -> float | None:
        if self._failed:
            return None
        try:
            if self._chan is None:
                lib, uri = getattr(self.radio, "_lib", None), getattr(self.radio, "uri", None)
                if lib is None or uri is None:
                    return None
                phy = lib.Context(uri).find_device("ad9361-phy")
                chan = phy.find_channel("temp0", False) if phy is not None else None
                if chan is None:
                    self._failed = True
                    return None
                self._chan = chan
            return round(float(self._chan.attrs["input"].value) / 1000.0, 1)
        except Exception as e:                      # noqa: BLE001 - telemetry only
            log.info("linktest: Pluto temperature unreadable (%s); not asked again", e)
            self._failed = True
            return None


#: Link counters worth sending as they change (DataLink.stats keys).
LINK_COUNTERS = ("t1_expired", "retransmitted", "sent_I", "sent_RR", "sent_RNR", "sent_REJ", "sent_SREJ",
                 "sent_XID", "sent_mode_request", "sent_SABM", "sent_SABME", "sent_DISC", "sent_DM", "sent_UA")
#: Radio counters a transmission is judged by (radios/pluto.py, base.py).
TX_COUNTERS = ("tx_late", "tx_late_ms", "tx_gaps", "keyed", "refused", "watchdog", "radio-error")


class Telemetry:
    """Streams this station's view of the link through `client` (module
    docstring), from the engine's taps and a once-a-second look at its
    links, receivers and radio."""

    def __init__(self, engine, client: RelayClient, radio=None, *, interval_s: float = 1.0):
        self.engine = engine
        self.client = client
        self.radio = radio if radio is not None else engine.radio
        self.interval_s = interval_s
        self._temp = PlutoTemperature(self.radio) if self.radio is not None else None
        self._temp_at = -1e9
        self._last_temp: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._overs: collections.deque = collections.deque()
        self._radio_mark = self._radio_counters()
        self._link_marks: dict[str, collections.Counter] = {}
        self._adapt_seen: dict[str, int] = {}
        self._heard_by_mode = collections.Counter()
        self._last_clock = 0.0
        #: The other station's latest telemetry by kind, and its rx-frames
        #: (the newest RX_KEEP): what the app's live view draws.
        self.peer: dict[str, dict] = {}
        self.peer_frames: collections.deque = collections.deque(maxlen=200)
        self.sent = collections.Counter()
        prev = client.on_tele

        def on_tele(msg):
            if prev is not None:
                prev(msg)
            kind = msg.get("kind", "")
            self.peer[kind] = msg
            if kind == "rx-frame":
                self.peer_frames.append(msg)
        client.on_tele = on_tele

    def start(self) -> Telemetry:
        self._send("env", **environment(self.radio, app=self.client.env.get("app", ""),
                                        extra={"call": self.client.call, "role": self.client.role}))
        self.engine.taps.append(self._tap)
        self._thread = threading.Thread(target=self._run, name="linktest-telemetry", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.engine.taps.remove(self._tap)
        except ValueError:
            pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._flush_overs(force=True)
        self._interval()

    # -- from the engine (under its lock: queue only) -------------------------------------

    def _tap(self, kind: str, d: dict) -> None:
        if kind == "heard":
            self._rx_frame(d)
        elif kind == "transmitted":
            self._overs.append(d)
        elif kind == "event":
            e = d["event"]
            conn = e.connection
            self._send("link", event=e.kind, reason=e.reason,
                       remote=str(conn.remote) if conn is not None else None, detail=_plain(e.detail))

    def _rx_frame(self, d: dict) -> None:
        frame = d["frame"]
        via = d.get("via", "radio")
        mode = d.get("mode") or (self.engine.channel.mode.name if via == "radio" else None)
        cn0 = d.get("cn0")
        snr = None
        if cn0 is not None and mode:
            try:
                from . import modem
                snr = cn0 - 10.0 * math.log10(modem.mode(mode).occupied_hz)
            except Exception:                       # noqa: BLE001 - a mode with no bandwidth
                snr = None
        if via == "radio":
            self._heard_by_mode[mode or "?"] += 1
        c = frame.control
        level = offset = None
        if via == "radio" and self.radio is not None:
            level = _num(getattr(self.radio, "level_db", None))
            offset = _num(getattr(getattr(self.radio, "receiver", None), "freq_hz", None))
        self._send("rx-frame", via=via, mode=mode, cn0_dbhz=_num(cn0), snr_db=_num(snr), level_db=level,
                   offset_hz=offset, fcs="ok", decode_lag_s=_num(d.get("decode_lag_s")), type=c.type.name,
                   ns=c.ns, nr=c.nr, pf=c.pf, src=str(frame.source), dst=str(frame.destination),
                   len=len(frame.info), frame=brief(frame), at=_num(d.get("t")))

    # -- once a second ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._flush_overs()
                self._interval()
            except Exception:                       # noqa: BLE001 - telemetry never stops the station
                log.exception("linktest telemetry")

    def _flush_overs(self, force: bool = False) -> None:
        """A transmission's report once it has played, with the radio's
        counters over it."""
        now = self.engine.clock()
        while self._overs and (force or now >= self._overs[0]["end"] + 0.3):
            d = self._overs.popleft()
            frames = [ax25.parse(f) for f in d["frames"]]
            counters = self._radio_counters()
            delta = {k: round(counters.get(k, 0) - self._radio_mark.get(k, 0), 3) for k in TX_COUNTERS
                     if counters.get(k, 0) != self._radio_mark.get(k, 0)}
            self._radio_mark = counters
            st = getattr(self.radio, "stats", {}) or {}
            types = collections.Counter(f.control.type.name if f is not None else "?" for f in frames)
            self._send("tx-over", start=_num(d["start"]), end=_num(d["end"]),
                       duration_s=_num(d["end"] - d["start"]), frames=len(frames), types=dict(types),
                       mode=getattr(self.radio, "mode", None) or self.engine.channel.mode.name,
                       modes=list(getattr(self.radio, "modes", None) or []) or None, counters=delta,
                       tx_slack_min_ms=_num(st.get("tx_slack_min_ms")),
                       tx_gain_db=_num(getattr(self.radio, "tx_gain_db", None)),
                       xo=getattr(self.radio, "xo_applied", None), temp_c=self._temperature(),
                       guard=_safe(lambda: self.radio.guard_state), first=[brief(f) for f in frames[:4]])

    def _interval(self) -> None:
        eng = self.engine
        with eng.lock:
            conns = list(eng.connections.values())
            links = []
            for conn in conns:
                link = conn.link
                key = str(conn.remote)
                mark = self._link_marks.setdefault(key, collections.Counter())
                now_counts = {k: link.stats.get(k, 0) for k in LINK_COUNTERS}
                changed = {k: v - mark.get(k, 0) for k, v in now_counts.items() if v != mark.get(k, 0)}
                mark.clear()
                mark.update(now_counts)
                links.append(dict(remote=key, state=link.state.value, vs=link.vs, va=link.va, vr=link.vr,
                                  rc=link.rc, srt_s=_num(link.srt), t1v_s=_num(link.t1v), queued=link.queued,
                                  k=link.k, n1=link.n1, modulo=link.modulo, srej=link.srej,
                                  tx_mode=link.tx_mode, peer_busy=link.peer_busy, counters=changed,
                                  sent_bytes=conn.sent_bytes, received_bytes=conn.received_bytes,
                                  via_return_path=conn.return_path is not None))
                adapter = link.adapter
                if adapter is not None:
                    seen = self._adapt_seen.get(key)
                    if seen is None or seen > len(adapter.history):
                        # A new adapter (connected or reconnected): what it
                        # asks for before any decision of its own.
                        self._send("adapt", remote=key, at=_num(eng.clock()), request=adapter.request,
                                   why="initial", ladder=list(adapter.modes) if hasattr(adapter, "modes") else None)
                        seen = 0
                    for t, request, why in adapter.history[seen:]:
                        self._send("adapt", remote=key, at=_num(t), request=request, why=why)
                    self._adapt_seen[key] = len(adapter.history)
            receivers = [eng.receiver] if eng.receiver is not None else list(eng.receivers.values())
            names = ["main"] if eng.receiver is not None else list(eng.receivers.keys())
            rx = {name: {"frames": getattr(r, "frames", None), "fx25": getattr(r, "fx25_frames", None),
                         "bad_fcs": _bad_fcs(r)} for name, r in zip(names, receivers)}
            stats = {k: v for k, v in eng.stats.items()
                     if k in ("heard", "own_echo", "unparsed", "rp_sent", "rp_heard", "rp_refused", "rp_dropped")}
            ch = eng.channel.stats
            channel = {"transmissions": ch.transmissions, "frames": ch.frames, "airtime_s": _num(ch.airtime_s),
                       "refused": dict(ch.refused)}
        for item in links:
            self._send("link-stats", **item)
        self._send("rx", receivers=rx, heard_by_mode=dict(self._heard_by_mode), engine=stats, channel=channel,
                   level_db=_num(getattr(self.radio, "level_db", None)) if self.radio is not None else None,
                   rx_dropped=(getattr(self.radio, "stats", {}) or {}).get("rx_dropped"))
        if time.monotonic() - self._last_clock >= CLOCK_EVERY_S:
            self._last_clock = time.monotonic()
            est = self.client.clock()
            if est is not None:
                self._send("clock", relay_minus_local_s=est["offset_s"], rtt_s=est["rtt_s"], samples=est["samples"])

    # -- helpers ------------------------------------------------------------------------------

    def _radio_counters(self) -> dict:
        st = getattr(self.radio, "stats", None) or {}
        return {k: st.get(k, 0) for k in TX_COUNTERS}

    def _temperature(self) -> float | None:
        if self._temp is None:
            return None
        if time.monotonic() - self._temp_at >= TEMP_EVERY_S:
            self._temp_at = time.monotonic()
            self._last_temp = self._temp.read()
        return self._last_temp

    def _send(self, kind: str, **fields) -> None:
        self.sent[kind] += 1
        self.client.send({"t": "tele", "kind": kind, **_plain(fields)})


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return round(f, 6) if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _safe(fn):
    try:
        return fn()
    except Exception:                               # noqa: BLE001
        return None


def _bad_fcs(receiver) -> int | None:
    """Frames that failed their FCS in a receiver's framers (hdlc.Framer.
    bad_fcs, summed over its demodulators' slicers). Noise between overs
    makes these too: adapt.py's docstring says why they are a context, not
    a loss count."""
    total, found = 0, False
    for demod in getattr(receiver, "demods", []) or []:
        for slicer in getattr(demod, "slicers", []) or []:
            framer = getattr(slicer, "framer", None)
            if framer is not None and hasattr(framer, "bad_fcs"):
                total += framer.bad_fcs
                found = True
    for slicer in getattr(receiver, "slicers", []) or []:
        framer = getattr(slicer, "framer", None)
        if framer is not None and hasattr(framer, "bad_fcs"):
            total += framer.bad_fcs
            found = True
    return total if found else None
