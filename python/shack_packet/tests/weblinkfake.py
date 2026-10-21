"""A stand-in for openhamshack.com's link relay (openhamshack-os
server/link/lib.php) on 127.0.0.1, for the client's tests: the same four
operations and answers, kept in memory. The PHP has its own tests, and
openhamshack-os tools/test_link_server.py runs the real client against the
real PHP; this is so ohs-libs and the app can test without podman.

`latency_s` delays every answer (an HTTPS round trip to the site), `kill()`
stops answering at all (the site gone), `quiet(call)` stops the server
hearing from a station without it knowing."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def norm(call: str) -> str:
    c = (call or "").strip().upper()
    return c[:-2] if c.endswith("-0") else c


class FakeLinkServer:
    def __init__(self, latency_s: float = 0.0):
        self.latency_s = latency_s
        self.lock = threading.Condition()
        self.slots: dict[tuple, dict] = {}            # (pair, call) -> slot
        self.queues: dict[tuple, list] = {}           # (pair, call) -> messages for call
        self.seen: dict[tuple, float] = {}
        self.muted: set[str] = set()
        self.log: list[dict] = []
        self.dead = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if server.dead:
                    self.connection.close()
                    return
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, out = server.handle(req)
                if server.latency_s:
                    time.sleep(server.latency_s)
                if server.dead:
                    self.connection.close()
                    return
                body = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/link/link.php"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.dead = True
        self.httpd.shutdown()
        self.httpd.server_close()

    def kill(self):
        self.dead = True
        with self.lock:
            self.lock.notify_all()

    def quiet(self, call: str, on: bool = True):
        (self.muted.add if on else self.muted.discard)(norm(call))

    # -- the protocol ------------------------------------------------------------

    def pair(self, a, b):
        return hashlib.sha256("|".join(sorted((a, b))).encode()).hexdigest()[:24]

    def handle(self, req):
        op = req.get("op")
        call, peer = norm(req.get("call", "")), norm(req.get("peer", ""))
        pid = self.pair(call, peer)
        with self.lock:
            if op == "hello":
                old = self.slots.get((pid, call))
                tok = hashlib.sha256(req["token"].encode()).hexdigest()
                self.slots[(pid, call)] = {"token": tok, "hello": {"call": call, "peer": peer, "env": req.get("env"),
                                                                   "version": req.get("version")}, "left": False}
                self.queues[(pid, call)] = []
                self.seen[(pid, call)] = time.monotonic()
                other = self.slots.get((pid, peer))
                if other and not other["left"]:
                    q = self.queues[(pid, peer)]
                    if old and not old["left"]:
                        q.append({"t": "peer", "state": "left", "why": "replaced"})
                    q.append({"t": "peer", "state": "joined", "gen": tok[:8], "hello": self.slots[(pid, call)]["hello"]})
                    self.lock.notify_all()
                self.log.append({"event": "join", "call": call})
                return 200, {"ok": True, "pair": pid, "call": call, "peer_call": peer, "first": min(call, peer),
                             "cursor": 0, "peer": self.view(pid, peer), "relay_wall": time.time()}
            slot = self.slots.get((pid, call))
            if slot is None or slot["token"] != hashlib.sha256(req.get("token", "").encode()).hexdigest():
                return 409, {"ok": False, "error": "superseded"}
            if slot["left"]:
                return 409, {"ok": False, "error": "left"}
            if call not in self.muted:
                self.seen[(pid, call)] = time.monotonic()
            if op == "send":
                other = self.slots.get((pid, peer))
                here = bool(other and not other["left"])
                for m in req.get("msgs", []):
                    self.log.append({"from": call, "msg": m})
                    if here:
                        self.queues[(pid, peer)].append(m)
                self.lock.notify_all()
                return 200, {"ok": True, "n": len(req.get("msgs", [])), "delivered": here, "relay_wall": time.time()}
            if op == "poll":
                q = self.queues[(pid, call)]
                cur = req.get("cursor", 0)
                end = time.monotonic() + float(req.get("wait", 0))
                while len(q) <= cur and time.monotonic() < end and not self.dead:
                    if self.slots.get((pid, call)) is not slot:
                        return 409, {"ok": False, "error": "superseded"}
                    self.lock.wait(min(0.5, max(0.01, end - time.monotonic())))
                    if call not in self.muted:
                        self.seen[(pid, call)] = time.monotonic()
                if self.slots.get((pid, call)) is not slot:
                    return 409, {"ok": False, "error": "superseded"}
                msgs = q[cur:]
                return 200, {"ok": True, "msgs": msgs, "cursor": len(q), "peer": self.view(pid, peer),
                             "relay_wall": time.time()}
            if op == "bye":
                slot["left"] = True
                other = self.slots.get((pid, peer))
                if other and not other["left"]:
                    self.queues[(pid, peer)].append({"t": "peer", "state": "left", "why": req.get("why", "bye")})
                    self.lock.notify_all()
                return 200, {"ok": True}
        return 400, {"ok": False, "error": "bad-op"}

    def view(self, pid, peer):
        s = self.slots.get((pid, peer))
        if not s or s["left"]:
            return {"state": "absent"}
        return {"state": "here", "gen": s["token"][:8], "hello": s["hello"],
                "seen_s": round(time.monotonic() - self.seen[(pid, peer)], 1)}
