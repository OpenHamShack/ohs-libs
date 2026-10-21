"""The pad as a client of an app's transmit lease (CONTROLLER-PAD.md §7).

The pad never keys anything itself. A `tx` action calls the app's ordinary
lease - the one Remote uses, REMOTE-API §7, CONTROLLER-CONTRACT.md §6.1 -
so every check the app makes for any client (level, transmit switch,
licence and band plan, broker claim) applies to the pad too.

TxLease is that lease as the library sees it. On the platform the edition
implements it over HTTP (`POST api/ptt {"on": true, "remote": "pad-…"}`,
`POST api/ptt/heartbeat`, `POST api/ptt {"on": false}`); on a desktop the
app hands over an object that calls its own lease in process. LocalLease
below is a reference in-process lease with the REMOTE-API timings, for a
desktop app that has none yet and for the tests.

PadTx is the pad's side, one per open pad:
- `ptt` (hold): key on press, unkey on release, heartbeat every 1 s between.
- `transmit` (arm): first press arms for ARM_SECONDS, a second press within
  them keys, and on air one press only unkeys (the box's K1, pad.py).
- abort(): the device has gone while keyed. Unkey at once, before anything
  else - the box's Controller.serve() closed the device and never unkeyed
  (pad.py:934-940), leaving the carrier to the app's 5 s lease. That is the
  bug this module exists to make impossible.

Heartbeats are driven by tick(), which the input host calls from its read
loop at least every 0.25 s. No thread of its own on purpose: if the loop
wedges, the heartbeats stop and the app's lease unkeys within 5 s, which is
the safe direction. A heartbeat thread would keep a wedged pad on the air.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .i18n import _

ARM_SECONDS = 4.0
HEARTBEAT_SECONDS = 1.0
LEASE_SECONDS = 5.0


class TxLease(Protocol):
    def key(self, remote: str) -> dict:
        """Ask to go on air. {"onAir": bool, "txError": str | None}; a refusal
        is onAir false with the reason, not an exception (REMOTE-API §7: a
        refused key-up is still 200)."""

    def heartbeat(self, remote: str) -> bool:
        """Keep the lease. False: the caller no longer holds it (409), the
        app has already unkeyed."""

    def unkey(self, remote: str) -> None:
        """Stop. Any unkey ends the lease."""


@dataclass
class Keyed:
    app: str            # kind of the app the pad keyed
    action: str         # ptt | transmit
    control: str
    lease: TxLease
    since: float
    beat: float


class PadTx:
    """The transmit state of one pad: what it keyed, what is armed, and the
    last refusal to show on the key."""

    def __init__(self, remote: str, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None):
        self.remote = remote
        self.clock = clock
        self.log = log or (lambda m: None)
        self.keyed: Keyed | None = None
        self.armed: tuple[str, str, float] | None = None   # (app, control, until)
        self.refused: tuple[str, str, float] | None = None  # (app, reason, until)
        self.lock = threading.RLock()

    # Presses ------------------------------------------------------------------------
    def press(self, app: str, action: str, control: str, lease: TxLease, mode: str, on_air: bool) -> None:
        """A press on a control holding a tx action. mode 'hold' or 'arm';
        on_air: the app's own status says it is on air (keyed by anyone)."""
        now = self.clock()
        with self.lock:
            if mode == "hold":
                if self.keyed is None:
                    self._key(app, action, control, lease, now)
                return
            # arm: on air, one press only stops - whoever keyed it.
            if self.keyed is not None or on_air:
                self.armed = None
                self._unkey(lease if self.keyed is None else self.keyed.lease)
            elif self.armed and self.armed[0] == app and now < self.armed[2]:
                self.armed = None
                self._key(app, action, control, lease, now)
            else:
                self.armed = (app, control, now + ARM_SECONDS)

    def release(self, control: str) -> None:
        """A release: ends a held ptt keyed from this control."""
        with self.lock:
            k = self.keyed
            if k is not None and k.action == "ptt" and k.control == control:
                self._unkey(k.lease)

    def _key(self, app: str, action: str, control: str, lease: TxLease, now: float) -> None:
        try:
            answer = lease.key(self.remote) or {}
        except Exception as exc:          # the app unreachable is a refusal, shown on the key
            answer = {"onAir": False, "txError": str(exc) or type(exc).__name__}
        if answer.get("onAir"):
            # Heartbeats start only after the key-up has answered (REMOTE-API §7).
            self.keyed = Keyed(app, action, control, lease, now, self.clock())
            self.refused = None
        else:
            self.refused = (app, str(answer.get("txError") or ""), now + 2.5)

    def _unkey(self, lease: TxLease) -> None:
        self.keyed = None
        try:
            lease.unkey(self.remote)
        except Exception as exc:
            # Nothing more the pad can do; the app's lease ends it within 5 s
            # once heartbeats stop, and they just did.
            self.log(f"unkey failed: {exc}")

    # Upkeep -------------------------------------------------------------------------
    def tick(self, app_on_air: Callable[[str], bool | None] | None = None) -> None:
        """Heartbeat if due. app_on_air(kind): the app's latest status; False
        means it unkeyed by itself (time-out, a console press), so the pad
        stops holding a lease that no longer exists."""
        now = self.clock()
        with self.lock:
            if self.armed and now >= self.armed[2]:
                self.armed = None
            k = self.keyed
            if k is None:
                return
            if app_on_air is not None and app_on_air(k.app) is False:
                self.keyed = None
                return
            if now - k.beat >= HEARTBEAT_SECONDS:
                k.beat = now
                try:
                    held = k.lease.heartbeat(self.remote)
                except Exception as exc:
                    # A heartbeat that fails is not a reason to unkey: the
                    # lease decides, at 5 s. Try again next tick.
                    self.log(f"heartbeat failed: {exc}")
                    return
                if not held:
                    self.keyed = None

    def abort(self) -> bool:
        """The pad has gone (or is being closed) while it may be keyed: unkey
        first, and only then may the caller close anything. True if it had
        keyed something. A pad that comes back never re-keys: nothing here
        remembers what was held."""
        with self.lock:
            self.armed = None
            k = self.keyed
            if k is None:
                return False
            self._unkey(k.lease)
            return True

    # For faces ----------------------------------------------------------------------
    def is_armed(self, app: str) -> bool:
        with self.lock:
            return bool(self.armed and self.armed[0] == app and self.clock() < self.armed[2])

    def refusal(self, app: str) -> str | None:
        with self.lock:
            if self.refused and self.refused[0] == app and self.clock() < self.refused[2]:
                return self.refused[1]
            return None

    def keyed_app(self) -> str | None:
        with self.lock:
            return self.keyed.app if self.keyed else None

    def holding(self) -> bool:
        """A momentary ptt is held down: app switches wait for its release."""
        with self.lock:
            return bool(self.keyed and self.keyed.action == "ptt")


class LocalLease:
    """A reference in-process transmit lease with REMOTE-API §7's rules, for a
    desktop app that has no lease object of its own yet.

    on_key() is the app's own "go on air" (it runs every check it would for
    any client and returns None, or the refusal's reason); on_unkey() stops
    RF. The lease unkeys by itself after LEASE_SECONDS without a heartbeat:
    check() does that, and the app calls it from any timer, or start() runs
    a watchdog thread that does.
    """

    def __init__(self, on_key: Callable[[], str | None], on_unkey: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic, timeout: float = LEASE_SECONDS):
        self.on_key, self.on_unkey, self.clock, self.timeout = on_key, on_unkey, clock, timeout
        self.holder: str | None = None
        self.on_air = False
        self.last = 0.0
        self.lock = threading.RLock()
        self._stop = threading.Event()

    def key(self, remote: str) -> dict:
        with self.lock:
            if self.on_air:
                if self.holder == remote:
                    self.last = self.clock()
                    return {"onAir": True, "txError": None}
                return {"onAir": False, "txError": _("already on air from another client")}
            reason = self.on_key()
            if reason:
                return {"onAir": False, "txError": reason}
            self.on_air, self.holder, self.last = True, remote, self.clock()
            return {"onAir": True, "txError": None}

    def heartbeat(self, remote: str) -> bool:
        with self.lock:
            if not self.on_air or self.holder != remote:
                return False
            self.last = self.clock()
            return True

    def unkey(self, remote: str | None = None) -> None:
        with self.lock:
            if self.on_air:
                self.on_air, self.holder = False, None
                self.on_unkey()

    def check(self) -> bool:
        """Unkey if the holder has been silent too long. True if it did."""
        with self.lock:
            if self.on_air and self.clock() - self.last >= self.timeout:
                self.unkey()
                return True
            return False

    def start(self, every: float = 0.25) -> None:
        def run():
            while not self._stop.wait(every):
                self.check()
        threading.Thread(target=run, daemon=True, name="shack-pad-lease").start()

    def stop(self) -> None:
        self._stop.set()
