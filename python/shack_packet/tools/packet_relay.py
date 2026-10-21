#!/usr/bin/env python3
"""The OpenHamShack packet link-test relay: pairs two stations by session
id, carries one's return-path frames and both stations' telemetry to the
other, and logs every message to a JSONL file per session.

Asked for by Brian on 18/10/2026 for the on-air test with GM0OTB: each
run is one RF direction and the receiver's answers go back by internet
(shack_packet.returnpath, shack_packet.linktest is the client). It runs on
the OpenHamShack controller box, not in a desktop app (family rule: no
servers inside the desktop apps), and is reached on the tailnet only:

    packet_relay.py --listen 127.0.0.1:30480 --log-dir /var/lib/ohs-packet-relay/sessions
    tailscale serve --bg --tcp 30480 tcp://127.0.0.1:30480

(the systemd unit beside this file does the first). Stdlib only, Python
3.9+, one file, so the box needs nothing installed.

**Protocol 1**: one JSON object a line, UTF-8, at most 64 KiB a line
(LINK-TEST.md has every message). A client's first line is `hello`
(session, role "rf-sender" or "rf-receiver", call, peer, env); the relay
answers `welcome` (with the other station's hello if it is there) or
`error` and closes. One station per role per session. After that:

- `ping` -> `pong` with the relay's clocks (relay_wall, relay_mono), every
  second from each client: the heartbeat, and the clock offset estimate;
- `frame` and `tele` -> forwarded to the other station as they are (the
  relay adds nothing to them), and logged; with nobody to forward to, the
  sender gets `{"t": "undeliverable"}` and the log says so;
- `bye` -> the client leaves.
- The relay sends `peer` {state: joined, hello} / {state: left, why: bye,
  closed or timeout} to the station still there.

A client silent for SILENCE_S (5 s; it pings every second) is dropped as
"timeout". The relay keeps no state worth keeping across a restart: a
restart ends every session, and both stations see their return path lost.

**The log**: `<log-dir>/<UTC date>-<session>.jsonl`, created at the first
join and appended to for as long as the relay runs (a session joined
again later keeps writing the same file). One line per message received,
and per relay event:

    {"relay_wall": 1792345678.123456, "relay_mono": 1234.567891, "from": "rf-sender",
     "lag_s": 0.0213, "msg": {...the message as received...}}

`from` is the role, or "relay" for the relay's own events (join, leave,
refused, undeliverable, summary); `lag_s` is relay_wall minus the
message's own wall stamp: the one-way delay plus the clock difference.
When the last station leaves, a `summary` line is appended and the same
summary is written to `<same name>.summary.json`: the calls, joins and
leaves, the duration, messages by role and type, frames each way,
telemetry by kind, and each station's last clock estimate.

Files are 0644 in a 0755 folder: readable by the account the relay runs
as (shack, on the box) and anyone on it, never written by anyone else.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

LOG = logging.getLogger("packet-relay")
PROTOCOL = 1
ROLES = ("rf-sender", "rf-receiver")
MAX_LINE = 64 * 1024
SILENCE_S = 5.0
MAX_SESSIONS = 64
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
FORWARDED = ("frame", "tele")


def other(role: str) -> str:
    return ROLES[1] if role == ROLES[0] else ROLES[0]


class Session:
    def __init__(self, sid: str, log_dir: Path):
        self.sid = sid
        self.clients: dict[str, Client] = {}
        day = time.strftime("%Y%m%d", time.gmtime())
        self.path = log_dir / f"{day}-{sid}.jsonl"
        self.log = open(self.path, "a", encoding="utf-8", buffering=1)
        os.chmod(self.path, 0o644)
        self.started = time.time()
        self.calls: dict[str, str] = {}
        self.joins: list[dict] = []
        self.by_role: dict[str, collections.Counter] = {r: collections.Counter() for r in ROLES}
        self.tele: dict[str, collections.Counter] = {r: collections.Counter() for r in ROLES}
        self.clock: dict[str, dict] = {}

    def write(self, source: str, msg: dict) -> None:
        now = time.time()
        rec = {"relay_wall": round(now, 6), "relay_mono": round(time.monotonic(), 6), "from": source}
        wall = msg.get("wall")
        if isinstance(wall, (int, float)):
            rec["lag_s"] = round(now - wall, 6)
        rec["msg"] = msg
        try:
            self.log.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except (OSError, ValueError) as e:
            LOG.error("session %s: log write failed: %s", self.sid, e)

    def count(self, role: str, msg: dict) -> None:
        t = str(msg.get("t"))
        self.by_role[role][t] += 1
        if t == "tele":
            kind = str(msg.get("kind"))
            self.tele[role][kind] += 1
            if kind == "clock":
                self.clock[role] = {k: msg.get(k) for k in ("relay_minus_local_s", "rtt_s", "samples", "wall")}

    def summary(self) -> dict:
        ended = time.time()
        return {
            "t": "summary", "session": self.sid, "log": self.path.name,
            "started": round(self.started, 3), "ended": round(ended, 3),
            "duration_s": round(ended - self.started, 3), "calls": self.calls, "joins": self.joins,
            "messages": {r: dict(c) for r, c in self.by_role.items()},
            "frames": {f"{r}->{other(r)}": self.by_role[r]["frame"] for r in ROLES},
            "telemetry": {r: dict(c) for r, c in self.tele.items()},
            "clock": self.clock,
        }

    def close_summary(self) -> None:
        s = self.summary()
        self.write("relay", s)
        try:
            out = self.path.with_name(self.path.stem + ".summary.json")
            out.write_text(json.dumps(s, indent=1) + "\n")
            os.chmod(out, 0o644)
        except OSError as e:
            LOG.error("session %s: summary not written: %s", self.sid, e)
        LOG.info("session %s: summary %s", self.sid, json.dumps(s["frames"]))


class Client:
    def __init__(self, relay: Relay, reader, writer):
        self.relay = relay
        self.reader = reader
        self.writer = writer
        self.role = ""
        self.session: Session | None = None
        self.hello: dict = {}
        self.last_in = time.monotonic()
        self.why_left = "closed"
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"

    async def send(self, msg: dict) -> None:
        try:
            self.writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            await asyncio.wait_for(self.writer.drain(), timeout=SILENCE_S)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            self.close()

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:                           # noqa: BLE001
            pass


class Relay:
    def __init__(self, log_dir: Path, silence_s: float = SILENCE_S):
        self.log_dir = log_dir
        self.silence_s = silence_s
        self.sessions: dict[str, Session] = {}

    async def serve(self, host: str, port: int):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_server(self.handle, host, port, limit=MAX_LINE + 2)
        LOG.info("listening on %s, logging to %s", ", ".join(str(s.getsockname()) for s in server.sockets),
                 self.log_dir)
        return server

    async def handle(self, reader, writer) -> None:
        client = Client(self, reader, writer)
        try:
            await self._run(client)
        except Exception:                           # noqa: BLE001 - one client never stops the relay
            LOG.exception("client %s", client.peer)
        finally:
            await self._leave(client)
            client.close()

    async def _line(self, client: Client) -> dict | None:
        try:
            line = await asyncio.wait_for(client.reader.readline(), timeout=self.silence_s)
        except asyncio.TimeoutError:
            client.why_left = "timeout"
            return None
        except (ValueError, asyncio.LimitOverrunError):
            client.why_left = "line-too-long"
            return None
        except (OSError, ConnectionError):
            return None
        if not line:
            return None
        client.last_in = time.monotonic()
        try:
            msg = json.loads(line)
        except ValueError:
            return {"t": "?bad-json", "raw": line[:200].decode("utf-8", "replace")}
        return msg if isinstance(msg, dict) else {"t": "?not-object"}

    async def _run(self, client: Client) -> None:
        hello = await self._line(client)
        if hello is None:
            return
        refusal = self._check(hello)
        if refusal:
            LOG.info("refused %s: %s", client.peer, refusal)
            await client.send({"t": "error", "reason": refusal})
            sid = hello.get("session")
            if refusal == "role-taken" and sid in self.sessions:
                self.sessions[sid].write("relay", {"t": "refused", "reason": refusal, "hello": hello,
                                                   "from_addr": client.peer})
            return
        sid, role = hello["session"], hello["role"]
        sess = self.sessions.get(sid)
        if sess is None:
            sess = self.sessions[sid] = Session(sid, self.log_dir)
        client.session, client.role, client.hello = sess, role, hello
        sess.clients[role] = client
        sess.calls[role] = str(hello.get("call", ""))
        sess.joins.append({"role": role, "call": hello.get("call"), "joined": round(time.time(), 3),
                           "addr": client.peer})
        sess.write(role, hello)
        sess.count(role, hello)
        sess.write("relay", {"t": "join", "role": role, "addr": client.peer})
        LOG.info("session %s: %s %s joined from %s", sid, role, hello.get("call"), client.peer)
        peer = sess.clients.get(other(role))
        await client.send({"t": "welcome", "v": PROTOCOL, "session": sid, "role": role,
                           "peer": peer.hello if peer else None, "log": sess.path.name,
                           "relay_wall": time.time(), "relay_mono": time.monotonic()})
        if peer is not None:
            await peer.send({"t": "peer", "state": "joined", "hello": hello})
        while True:
            msg = await self._line(client)
            if msg is None:
                return
            sess.write(role, msg)
            sess.count(role, msg)
            t = msg.get("t")
            if t == "ping":
                await client.send({"t": "pong", "n": msg.get("n"), "wall": msg.get("wall"),
                                   "relay_wall": time.time(), "relay_mono": time.monotonic()})
            elif t in FORWARDED:
                peer = sess.clients.get(other(role))
                if peer is None:
                    sess.write("relay", {"t": "undeliverable", "role": role, "of": t, "n": msg.get("n")})
                    if t == "frame":
                        await client.send({"t": "undeliverable", "n": msg.get("n")})
                else:
                    await peer.send(msg)
            elif t == "bye":
                client.why_left = "bye"
                return

    def _check(self, hello: dict) -> str | None:
        if hello.get("t") != "hello":
            return "hello-first"
        if hello.get("v") != PROTOCOL:
            return "protocol"
        sid = hello.get("session")
        if not isinstance(sid, str) or not SESSION_RE.match(sid):
            return "bad-session"
        if hello.get("role") not in ROLES:
            return "bad-role"
        sess = self.sessions.get(sid)
        if sess is not None and hello["role"] in sess.clients:
            return "role-taken"
        if sess is None and len(self.sessions) >= MAX_SESSIONS:
            return "too-many-sessions"
        return None

    async def _leave(self, client: Client) -> None:
        sess = client.session
        if sess is None or sess.clients.get(client.role) is not client:
            return
        del sess.clients[client.role]
        sess.write("relay", {"t": "leave", "role": client.role, "why": client.why_left})
        LOG.info("session %s: %s left (%s)", sess.sid, client.role, client.why_left)
        peer = sess.clients.get(other(client.role))
        if peer is not None:
            await peer.send({"t": "peer", "state": "left", "why": client.why_left})
        else:
            sess.close_summary()
            sess.log.close()
            del self.sessions[sess.sid]


def parse_listen(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="127.0.0.1:30480", help="host:port (default 127.0.0.1:30480)")
    p.add_argument("--log-dir", default="/var/lib/ohs-packet-relay/sessions")
    p.add_argument("--silence-s", type=float, default=SILENCE_S)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    host, port = parse_listen(a.listen)
    relay = Relay(Path(a.log_dir), a.silence_s)

    async def run():
        server = await relay.serve(host, port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        async with server:
            await stop.wait()
        for sess in list(relay.sessions.values()):
            sess.write("relay", {"t": "relay-stopping"})
            sess.close_summary()
            sess.log.close()
        LOG.info("stopped")

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
