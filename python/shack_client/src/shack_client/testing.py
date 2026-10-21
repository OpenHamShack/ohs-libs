"""A reference controller for tests: the sync, pairing and discovery
endpoints a client needs, over the same shack_client.controller.SyncService
the Dock360 edition mounts (SYNC.md §10), so the client is tested against
the controller's real merge, sequence and epoch logic rather than a mock.

Two ways to reach it:
- `serve()` runs it on http.server on a loopback port, for tests that need
  real HTTP (timeouts, a dropped connection, discovery).
- `Network` + `DirectTransport` call `handle()` in-process with no socket,
  so the shared vectors run fast; the routing and JSON are the same code.

Fault knobs, for the cases SYNC.md §8 names: `drop_next_push_answer` (the
push is applied and the answer lost: T13), `offline` (no answer at all),
`restore()` (a backup restored: T15). Users have a password, an optional
TOTP code and whether they hold Transmit anywhere (profile edits: T21).

Not a controller: no levels beyond that one, no broker, no TV approval
UI. It speaks exactly as much of CONTROLLER-CONTRACT.md as shack_client uses.
"""
from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import hlc as H
from .controller import SyncService
from .transport import Offline


class Dropped(Offline):
    """The request reached the controller; the answer never came back."""


class ReferenceController:
    def __init__(self, path: str | Path, name: str = "GM4BRI", controller_id: str | None = None,
                 now_ms=H.system_ms, users: dict | None = None):
        self.name = name
        self.controller_id = controller_id or str(uuid.uuid4())
        self.now_ms = now_ms
        self.sync = SyncService(path, self.controller_id, now_ms=lambda: self.now_ms())
        # username -> {"password", "totp" (optional), "transmit" (bool)}
        self.users = users if users is not None else {"brian": {"password": "pw", "transmit": True}}
        self.devices: dict[str, dict] = {}         # token -> device
        self.tv_pairs: dict[str, dict] = {}
        self.drop_next_push_answer = False
        self.offline = False
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------- the routes
    def handle(self, method: str, path: str, headers: dict, body: dict | None) -> tuple[int, dict]:
        """One request: (status, JSON). `path` may carry a query string."""
        self.requests.append((method, path))
        u = urllib.parse.urlsplit(path)
        route = u.path.lstrip("/")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if method == "GET" and route == ".well-known/openhamshack":
            return 200, self.well_known()
        if method == "POST" and route == "api/shack/token":
            return self._token(body or {})
        if method == "POST" and route == "api/shack/pair/start":
            return self._tv_start(body or {})
        if method == "POST" and route == "api/shack/pair/poll":
            return self._tv_poll(body or {})
        dev = self._device(headers)
        if dev is None:
            return 401, {"error": "Sign in to this controller first.", "code": "credentials_required"}
        if dev.get("revoked"):
            return 401, {"error": f"This device has been removed from {self.name}.", "code": "token_revoked"}
        if method == "GET" and route == "api/shack/v1/":
            return 200, self.sync.info()
        if method == "POST" and route == "api/shack/v1/sync/push":
            return self.sync.push(body or {}, can_edit_profile=dev["transmit"])
        if method == "GET" and route == "api/shack/v1/sync/pull":
            cols = [c for c in q.get("collections", "").split(",") if c] or None
            return self.sync.pull(int(q.get("since", 0)), q.get("epoch") or None, int(q.get("limit", 500)), cols)
        if method == "POST" and route == "api/shack/v1/pair/approve":
            return self._tv_approve(body or {}, dev)
        return 404, {"error": "There is nothing at that address on this controller.", "code": "not_found"}

    def well_known(self) -> dict:
        return {"openhamshack": 1, "contract": "1.0", "controllerId": self.controller_id, "name": self.name,
                "version": "2.0.0-test", "time": H.iso_ms(self.now_ms()),
                "station": {"callsign": self.name, "locator": "", "country": ""},
                "auth": {"token": "api/shack/token", "pair": "api/shack/pair/start", "totp": "per-user",
                         "anonymousView": []},
                "services": {"base": "api/shack/v1/", "profile": 1, "logbook": 1, "sync": 1},
                "bandplan": {"dataVersion": "", "schema": 1}, "apps": []}

    def _device(self, headers: dict) -> dict | None:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Bearer "):
            return None
        return self.devices.get(auth[7:])

    def _mint(self, user: str, name: str, kind: str, client_id: str, max_level: str) -> tuple[str, dict]:
        with self._lock:
            for tok, d in list(self.devices.items()):
                if d["clientId"] == client_id and d["user"] == user and not d.get("revoked"):
                    d["revoked"] = True
            token = "shackdev_" + secrets.token_urlsafe(32)
            dev = {"id": "dev_" + secrets.token_hex(6).upper(), "user": user, "name": name, "kind": kind,
                   "clientId": client_id, "maxLevel": max_level,
                   "transmit": max_level == "transmit" and bool(self.users.get(user, {}).get("transmit"))}
            self.devices[token] = dev
        return token, dev

    def _token(self, body: dict) -> tuple[int, dict]:
        user = self.users.get(body.get("username", ""))
        if body.get("deviceKind") == "tv":
            return 400, {"error": "A TV pairs with a code, not a password.", "code": "use_tv_pairing"}
        if user is None or user["password"] != body.get("password"):
            return 401, {"error": "The username or password is wrong.", "code": "bad_credentials"}
        if user.get("totp") and body.get("totp") != user["totp"]:
            return 401, {"error": "Enter the code from your authenticator app.", "code": "totp_required"}
        token, dev = self._mint(body["username"], body.get("deviceName", ""), body.get("deviceKind", "desktop"),
                                body.get("clientId", ""), "transmit")
        return 201, {"token": token, "deviceId": dev["id"], "user": body["username"], "admin": False,
                     "maxLevel": "transmit", "controllerId": self.controller_id}

    def _tv_start(self, body: dict) -> tuple[int, dict]:
        pid = "pr_" + secrets.token_hex(3).upper()
        p = {"pairId": pid, "pairSecret": secrets.token_urlsafe(32), "code": f"{secrets.randbelow(10**6):06d}",
             "state": "pending", "body": body}
        self.tv_pairs[pid] = p
        return 200, {"pairId": pid, "pairSecret": p["pairSecret"], "code": p["code"], "expiresIn": 300, "pollEvery": 2}

    def _tv_approve(self, body: dict, dev: dict) -> tuple[int, dict]:
        p = next((x for x in self.tv_pairs.values() if x["code"] == body.get("code") and x["state"] == "pending"), None)
        if p is None:
            return 404, {"error": "No TV is showing that code.", "code": "not_found"}
        level = body.get("maxLevel", "view")
        token, tv = self._mint(dev["user"], p["body"].get("deviceName", ""), "tv", p["body"].get("clientId", ""),
                               "control" if level == "control" else "view")
        p.update(state="approved", token=token, device=tv)
        return 200, {"approved": True}

    def _tv_poll(self, body: dict) -> tuple[int, dict]:
        p = self.tv_pairs.get(body.get("pairId", ""))
        if p is None or p["pairSecret"] != body.get("pairSecret"):
            return 410, {"error": "That pairing has expired.", "code": "pair_expired"}
        if p["state"] == "approved":
            del self.tv_pairs[p["pairId"]]
            return 200, {"state": "approved", "token": p["token"], "deviceId": p["device"]["id"],
                         "user": p["device"]["user"], "maxLevel": p["device"]["maxLevel"]}
        return 200, {"state": p["state"]}

    # ---------------------------------------------------------- faults and state
    def restore(self, snapshot: bytes) -> None:
        self.sync.restore(snapshot)

    def snapshot(self) -> bytes:
        return self.sync.snapshot()

    def records(self, collection: str = "logbook") -> dict:
        return {r["id"]: r for r in self.sync.all(collection)}

    # ---------------------------------------------------------- real HTTP
    def serve(self, host: str = "127.0.0.1", port: int = 0) -> str:
        """Start on a loopback port; returns the base URL."""
        ctl = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                if ctl.offline:
                    self.close_connection = True
                    return
                status, ans = ctl.handle(method, self.path, dict(self.headers), body)
                if method == "POST" and self.path.startswith("/api/shack/v1/sync/push") and ctl.drop_next_push_answer:
                    ctl.drop_next_push_answer = False
                    self.close_connection = True
                    return
                raw = json.dumps(ans).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self._server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://{host}:{self._server.server_address[1]}/"

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self.sync.close()


class Network:
    """In-process controllers by base URL, and the transport factory a
    Station takes to reach them without sockets."""

    def __init__(self):
        self.controllers: dict[str, ReferenceController] = {}
        self.calls = 0

    def add(self, base_url: str, ctl: ReferenceController) -> None:
        self.controllers[base_url] = ctl

    def factory(self, base_url: str, token: str | None):
        return DirectTransport(self, base_url, token)


class DirectTransport:
    def __init__(self, net: Network, base_url: str, token: str | None):
        self.net, self.base_url, self.token = net, base_url, token

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        self.net.calls += 1
        ctl = self.net.controllers.get(self.base_url)
        if ctl is None or ctl.offline:
            raise Offline(f"{self.base_url} does not answer")
        p = path.lstrip("/")
        if query:
            p += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # A JSON round trip, so nothing shares mutable objects with the
        # controller that a real wire would not.
        status, ans = ctl.handle(method, p, headers, json.loads(json.dumps(body)) if body is not None else None)
        if method == "POST" and p.startswith("api/shack/v1/sync/push") and ctl.drop_next_push_answer:
            ctl.drop_next_push_answer = False
            raise Dropped("the answer to the push was lost")
        return status, json.loads(json.dumps(ans))
