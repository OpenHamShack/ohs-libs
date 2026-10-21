"""Fakes shared by the router, transmit and focus tests: a clock the test
moves, a lease that records every call, and an in-process app."""

from __future__ import annotations

import threading

from shack_pad import InProcessApp


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class RecordingLease:
    """The app's transmit lease as the pad sees it. Records ("key", remote),
    ("beat", remote), ("unkey", remote) into log, which a test may share with
    other recorders to check the order of things."""

    def __init__(self, log: list | None = None, refuse: str | None = None):
        self.log = log if log is not None else []
        self.refuse = refuse
        self.on_air = False
        self.holder: str | None = None
        self.lost = False
        self.lock = threading.Lock()

    def key(self, remote):
        with self.lock:
            self.log.append(("key", remote))
            if self.refuse:
                return {"onAir": False, "txError": self.refuse}
            self.on_air, self.holder = True, remote
            return {"onAir": True, "txError": None}

    def heartbeat(self, remote):
        with self.lock:
            self.log.append(("beat", remote))
            return not self.lost and self.on_air and self.holder == remote

    def unkey(self, remote):
        with self.lock:
            self.log.append(("unkey", remote))
            self.on_air, self.holder = False, None

    def calls(self, what):
        return [c for c in self.log if c[0] == what]


def make_app(kind, decl, layout, lease=None, name=None):
    performed = []
    state = {"faces": {}}
    lease = lease if lease is not None else RecordingLease()
    app = InProcessApp(kind, name or kind.upper(), decl, layout, perform=performed.append,
                       faces=lambda: {"faces": state["faces"], "onAir": lease.on_air}, lease=lease)
    app.performed, app.state, app.tx = performed, state, lease
    return app
