"""The link test's return path through openhamshack.com: two stations find
each other by callsign, and the receiver's answers and both stations'
telemetry go through a small PHP relay there (openhamshack-os
server/link; the protocol is "Web protocol 1" in LINK-TEST.md).

Why (Brian, 20/10/2026): the 0.2.1 relay is a TCP service on the
controller box, reachable on the tailnet only, and GM0OTB could not reach
it. openhamshack.com is reachable from every station that can fetch an
update, over plain outgoing HTTPS. It is shared hosting with no
long-running processes, so instead of one TCP connection each station
makes requests: one long poll at a time (held by the server until there is
something for us, at most `poll_wait_s`), and batches of outgoing messages
posted as they queue.

    client = WebRelayClient("https://openhamshack.com/api/link/link.php",
                            call="GM4BRI-1", peer="GM0OTB-1", role="rf-sender",
                            env=environment(radio, app="0.3.0"), log_path=...)
    client.start()                                 # hello; raises LinkTestError
    path = RelayReturnPath(client, "GM0OTB-1", outbound=False)
    ...
    client.set_role("rf-receiver")                 # the second half of the run

`WebRelayClient` is a `linktest.RelayClient` with another transport: the
same states ("waiting", "ready", "lost", "closed"), callbacks, telemetry,
clock estimate and log file, so `RelayReturnPath`, `Telemetry` and the
app's link test use either without knowing which. What differs:

- **Pairing is by callsign, not session.** The pair exists only when each
  station asked for the other (normalised, SSID-aware: the server's rule).
  `first` is the call that sorts first; the app uses it to decide who sends
  first, so both ends agree without a word.
- **A token per run.** 32 random hex characters, on every request; a new
  run of the same callsign replaces the old one at the server.
- **Roles are the app's business.** The server does not know them, so one
  pairing carries a whole run in both directions; `set_role` changes what
  this end calls itself in telemetry.
- **Silence.** The writer posts at least every `heartbeat_s` (an empty
  batch is a heartbeat, and its answer gives a clock sample). Nothing
  answered for `silence_s` loses the return path for good, as the TCP relay
  closing does: the links drop without transmitting (returnpath.py). The
  other station counts as gone when the server last heard from it more
  than `peer_silent_s` ago, and back when it is heard again.

**Outwards only**: requests from this station, nothing listens.
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import ssl
import threading
import time
import urllib.parse

from .linktest import LinkTestError, RelayClient, RelayReturnPath, stamp
from .returnpath import ReturnPath

log = logging.getLogger(__name__)

#: Web protocol version (LINK-TEST.md, "Web protocol 1").
WEB_PROTOCOL = 1
DEFAULT_URL = "https://openhamshack.com/api/link/link.php"
#: How long a poll may be held at the server; it caps this at 20.
POLL_WAIT_S = 15.0
#: An empty batch at least this often: liveness and clock.
WEB_HEARTBEAT_S = 2.0
#: Nothing answered for this long: the return path is lost. Longer than the
#: TCP relay's 5 s because an HTTPS request on a poor line can take a
#: second or two to come back on its own, and each lost run costs both
#: operators a restart; still short enough that a sender stops within a
#: few overs of the relay going away.
WEB_SILENCE_S = 8.0
#: The server last heard from the other station longer ago than this: gone.
#: Its long poll touches the server every 4 s and its heartbeat every 2 s.
PEER_SILENT_S = 15.0
#: Most messages in one POST (the server takes 400).
BATCH = 200
REQUEST_TIMEOUT_S = 10.0


class WebRelayClient(RelayClient):
    """RelayClient through openhamshack.com (module docstring)."""

    def __init__(self, url: str = DEFAULT_URL, *, call: str, peer: str, role: str, env: dict | None = None,
                 log_path=None, silence_s: float = WEB_SILENCE_S, heartbeat_s: float = WEB_HEARTBEAT_S,
                 poll_wait_s: float = POLL_WAIT_S, peer_silent_s: float = PEER_SILENT_S,
                 version: str = "", ssl_context: ssl.SSLContext | None = None):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise LinkTestError("bad-relay", f"not a web address: {url!r}")
        # The parent wants a session id; the pair stands in for it once known.
        super().__init__(u.hostname, u.port or (443 if u.scheme == "https" else 80), session="web-pending",
                         role=role, call=call, peer=peer, env=env, log_path=log_path, silence_s=silence_s,
                         heartbeat_s=heartbeat_s)
        self.url = url
        self._scheme, self._path = u.scheme, (u.path or "/") + (f"?{u.query}" if u.query else "")
        self._ssl = ssl_context
        self.poll_wait_s = poll_wait_s
        self.peer_silent_s = peer_silent_s
        self.version = version or str(self.env.get("app", ""))
        self.token = secrets.token_hex(16)
        self.first = ""
        self.pair = ""
        self.peer_call = ""
        self._cursor = 0
        self._peer_gen: str | None = None
        self._peer_quiet = False
        self._last_sent = 0.0
        self.rtts: list[float] = []
        self._poll_conn = None

    # -- life ------------------------------------------------------------------------

    def start(self) -> WebRelayClient:
        try:
            out = self._request(self._connection(), "hello", v=WEB_PROTOCOL, call=self.call, peer=self.peer,
                                token=self.token, version=self.version, env=self.env, **stamp())
        except LinkTestError:
            self._closed_log()
            raise
        self._logged("in", {"t": "welcome", **out})
        self.welcome = out
        self.pair = self.session = out.get("pair", "")
        self.first = out.get("first", "")
        self.call = out.get("call", self.call)
        self.peer_call = out.get("peer_call", self.peer)
        self._cursor = int(out.get("cursor", 0))
        self._last_in = time.monotonic()
        peer = out.get("peer") or {}
        if peer.get("state") == "here":
            self.peer_hello = peer.get("hello") or {}
            self._peer_gen = peer.get("gen")
        self._set_state("ready" if self.peer_hello is not None else "waiting", "joined")
        for name, target in (("poll", self._poll_loop), ("writer", self._write_loop)):
            t = threading.Thread(target=target, name=f"weblink-{name}", daemon=True)
            self._threads.append(t)
            t.start()
        return self

    def set_role(self, role: str) -> None:
        """The other half of a run (module docstring): roles are ours."""
        self.role = role

    def close(self, why: str = "closed") -> None:
        if self.state == "closed":
            return
        self._leaving = True
        # What is queued goes first (the last telemetry of a run), then bye.
        deadline = time.monotonic() + 2.0
        while not self._out.empty() and time.monotonic() < deadline and self.state in ("waiting", "ready"):
            time.sleep(0.02)
        was = self.state
        self.state = "closed"
        self._stop.set()
        self._out.put(None)
        if was in ("waiting", "ready"):
            try:
                self._request(self._connection(timeout=3.0), "bye", why=why)
            except LinkTestError:
                pass
        # The poll thread may be inside a long poll: cut its socket rather
        # than wait up to poll_wait_s for it (it checks _stop on the way out).
        self._shutdown()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=REQUEST_TIMEOUT_S + 1)
        if was != "closed" and self.on_state is not None:
            self.on_state("closed", why)
        self._closed_log()

    # -- requests ----------------------------------------------------------------------

    def _connection(self, timeout: float = REQUEST_TIMEOUT_S):
        if self._scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=self._ssl or ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _request(self, conn, op: str, **fields) -> dict:
        """One POST; the answer, or LinkTestError (`unreachable` when it did
        not get there, `refused` with the server's reason when it said no)."""
        body = json.dumps({"op": op, **fields, **({"token": self.token, "call": self.call, "peer": self.peer}
                                                   if op != "hello" else {})}, separators=(",", ":")).encode()
        try:
            conn.request("POST", self._path, body=body, headers={"Content-Type": "application/json",
                                                                 "User-Agent": f"OpenHamShack-link/{self.version}"})
            r = conn.getresponse()
            raw = r.read()
            status = r.status
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise LinkTestError("unreachable", f"{self.url}: {e}") from None
        try:
            out = json.loads(raw)
        except ValueError:
            out = {}
        if status != 200 or not out.get("ok"):
            raise LinkTestError("refused", f"{out.get('error') or status}")
        return out

    # -- threads ---------------------------------------------------------------------

    def _write_loop(self) -> None:
        conn = self._connection()
        while not self._stop.is_set():
            batch = []
            try:
                msg = self._out.get(timeout=self.heartbeat_s / 4)
            except Exception:                    # queue.Empty
                msg = False
            if msg is None:
                break
            if msg:
                batch.append(msg)
                while len(batch) < BATCH:
                    try:
                        more = self._out.get_nowait()
                    except Exception:
                        break
                    if more is None:
                        self._stop.set()
                        break
                    batch.append(more)
            now = time.monotonic()
            if not batch and now - self._last_sent < self.heartbeat_s:
                if now - self._last_in > self.silence_s:
                    self._lose("relay-silent", f"nothing from {self.host} for {self.silence_s:.0f} s")
                    break
                continue
            if self._post(conn, batch):
                continue
            # Not delivered: keep the batch at the front and try again until
            # the silence limit decides it is gone.
            if time.monotonic() - self._last_in > self.silence_s:
                if not self._leaving:
                    self._lose("relay-silent", f"nothing from {self.host} for {self.silence_s:.0f} s")
                break
            conn = self._connection()
            for m in reversed(batch):
                self._requeue(m)
            self._stop.wait(0.3)
        conn.close()

    def _requeue(self, msg) -> None:
        # queue.Queue has no appendleft; its deque does, under its mutex.
        with self._out.mutex:
            self._out.queue.appendleft(msg)
            self._out.not_empty.notify()

    def _post(self, conn, batch: list) -> bool:
        t0, w0 = time.monotonic(), time.time()
        try:
            out = self._request(conn, "send", msgs=batch)
        except LinkTestError as e:
            if e.detail in ("superseded", "left"):
                self._lose("refused", f"the relay says {e.detail}")
            elif e.reason == "refused":
                log.warning("weblink: a batch of %d refused: %s", len(batch), e.detail)
                self._last_in = time.monotonic()       # it answered: not silent
                return True                             # dropped, never retried
            return False
        t1, w1 = time.monotonic(), time.time()
        self._last_in = self._last_sent = t1
        rtt = t1 - t0
        self.rtts.append(rtt)
        del self.rtts[:-200]
        if "relay_wall" in out:
            self._clock.append((t1, out["relay_wall"] - (w0 + w1) / 2.0, rtt))
        for m in batch:
            self._logged("out", m)
        return True

    def _poll_loop(self) -> None:
        conn = self._poll_conn = self._connection(timeout=self.poll_wait_s + REQUEST_TIMEOUT_S)
        while not self._stop.is_set():
            try:
                out = self._request(conn, "poll", cursor=self._cursor, wait=self.poll_wait_s)
            except LinkTestError as e:
                if self._stop.is_set():
                    break
                if e.detail in ("superseded", "left", "bad-cursor"):
                    if not self._leaving:
                        self._lose("refused", f"the relay says {e.detail}")
                    break
                conn = self._poll_conn = self._connection(timeout=self.poll_wait_s + REQUEST_TIMEOUT_S)
                self._stop.wait(0.5)
                continue
            if self._stop.is_set():
                break
            self._last_in = time.monotonic()
            self._cursor = int(out.get("cursor", self._cursor))
            for msg in out.get("msgs") or []:
                self._logged("in", msg)
                if msg.get("t") == "peer":
                    self._peer_gen = msg.get("gen") if msg.get("state") == "joined" else None
                    self._peer_quiet = False
                try:
                    self._handle(msg)
                except Exception:                   # noqa: BLE001 - one bad message never ends the test
                    log.exception("weblink: handling %s", msg.get("t"))
            self._peer_presence(out.get("peer") or {})
        conn.close()

    def _peer_presence(self, peer: dict) -> None:
        """The other station going quiet, or coming back, from what the
        server last heard of it (module docstring)."""
        if peer.get("state") != "here":
            return
        seen = peer.get("seen_s")
        quiet = seen is not None and seen > self.peer_silent_s
        if quiet and not self._peer_quiet:
            self._peer_quiet = True
            self._handle({"t": "peer", "state": "left", "why": "timeout"})
        elif not quiet and self._peer_quiet:
            self._peer_quiet = False
            self._handle({"t": "peer", "state": "joined", "hello": peer.get("hello") or {}})

    def _shutdown(self) -> None:
        # Called by close and by RelayClient._lose: abort a long poll in
        # flight so its thread ends now.
        conn = self._poll_conn
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(2)
            except OSError:
                pass

    def describe(self) -> dict:
        return {"url": self.url, "pair": self.pair, "call": self.call, "peer": self.peer_call, "first": self.first}


class WebReturnPath(RelayReturnPath):
    """returnpath.ReturnPath through a WebRelayClient. Nothing to add to
    RelayReturnPath (the client keeps its contract); its own name so a
    monitor line and the telemetry can say which relay carried a frame."""

    name = "internet"

    def describe(self) -> dict:
        return {**ReturnPath.describe(self), **self.client.describe(), "role": self.client.role}
