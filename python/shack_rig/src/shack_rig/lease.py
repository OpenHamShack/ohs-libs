"""Transmit permission, as the rig library sees it: a lease the app hands in.

**This library never decides that a rig may transmit.** The app does, once,
in the place every client already goes through: its TX lease (REMOTE-API §7,
CONTROLLER-CONTRACT §6.1 - level, the transmit switch, licence and band plan,
the time-out) and the hardware broker's claim on this radio (PLATFORM-PLAN
§1.3). What it hands the library is the *result*: an object that answers
"may this holder transmit on this rig right now?".

So keying is one explicit call with that object in it -

    rig.set_ptt(True, lease)      # raises PttRefused without a valid lease
    rig.set_ptt(False)            # never needs one, never refused here

- and there is no other route to the transmitter in this library. Not a
default lease, not a module-wide "allow" that stands in for one, not a lease
remembered from an earlier key. The Icom app used to have exactly that: an
`allow_ptt` flag set from TX_ENABLED at start-up, after which any caller of
`set_ptt(True)` - the page, rigctl, the digital modes - keyed the radio. The
flag stays (`allow_ptt`, default False, a second gate an app sets from its
transmit switch), but it is no longer sufficient on its own.

**Why the rig asks again while keyed.** A lease is not a ticket checked at the
door. It lapses: the client stops heartbeating, the operator flips the
transmit switch, the broker hands the radio to another app. So the rig keeps
the lease it was keyed with and asks `refusal()` on every poll (four times a
second by default); the first non-None answer unkeys the radio. The app's own
lease timer would unkey it too, through whatever path it uses - this is the
library not relying on that path being healthy.

**Why unkeying never needs a lease.** Testing permission on both edges looks
symmetrical and ends with a key stuck down: unkey is the one command that must
always be attempted, especially when the module believes transmitting is off,
because that is the state it wakes up in after a restart it did not choose.

**What the library does *not* police:** a radio keyed from its own front panel
or its own microphone. That is the operator at the radio, and the rig reports
it in `state["ptt"]` without second-guessing it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol, runtime_checkable

from .i18n import _


class PttRefused(Exception):
    """Keying was asked for without permission. `reason` is for a person.

    An exception rather than False on purpose: False from `set_ptt` already
    means "the command did not reach the radio" (no link), which a caller may
    retry. A missing or lapsed lease is not retryable by trying again, and a
    caller that forgot to pass one has a bug that should be loud in a test,
    not a transmitter that quietly did not key.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@runtime_checkable
class TxLease(Protocol):
    """What an app hands a rig to key it. Implemented by the app, not here.

    `holder` names who is transmitting, for logs and for the rigctl server to
    tell clients apart ("rigctl 198.51.100.20:51234", "pad-3f2a", "digi FT8").

    `refusal()` answers None while the holder may transmit on this rig - its
    lease is held and unexpired, the transmit switch is on, the broker claim on
    this radio is still the app's - and otherwise one sentence saying which of
    those failed. It is called from the rig's poll loop, so it must be cheap
    and must not block: read state the app already keeps, do not ask the
    broker over HTTP on every call.
    """

    holder: str

    def refusal(self) -> str | None: ...


class Lease:
    """A reference TxLease: a time-limited grant with an optional claim check.

    For desktop apps that have no lease of their own yet, and for tests. An
    app on the platform passes its own lease object instead - this one knows
    nothing about the broker beyond the `claim` callable it is given, which
    should answer whether the app still holds the radio.

    The timings are REMOTE-API §7's: a lease lasts `seconds` from the last
    heartbeat (5 s) and must be renewed by the client about once a second.
    """

    def __init__(self, holder: str, seconds: float = 5.0,
                 claim: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.holder = holder
        self.seconds = seconds
        self._claim = claim
        self._clock = clock
        self._lock = threading.Lock()
        self._until = clock() + seconds
        self._revoked: str | None = None

    def heartbeat(self) -> bool:
        """Renew. False once the lease has lapsed or been revoked - a lapsed
        lease is not revived by a late heartbeat, the client must key again."""
        with self._lock:
            if self._revoked or self._clock() > self._until:
                return False
            self._until = self._clock() + self.seconds
            return True

    def revoke(self, reason: str = "the lease was ended") -> None:
        with self._lock:
            self._revoked = reason

    def release(self) -> None:
        """The holder is done. Same as revoke, with the ordinary reason."""
        self.revoke("the transmission ended")

    def refusal(self) -> str | None:
        with self._lock:
            if self._revoked:
                return self._revoked
            if self._clock() > self._until:
                return f"{self.holder}'s transmit lease expired"
        if self._claim is not None and not self._claim():
            return "another app holds this radio"
        return None


def check(lease: object) -> None:
    """Raise PttRefused unless `lease` is a TxLease that currently permits.

    The one test every keying path in this library runs, so there is one
    sentence for "no lease" and it cannot drift between the rig and rigctl.
    """
    if lease is None:
        raise PttRefused(_("transmitting needs a TX lease from the app, and none was given"))
    if not isinstance(lease, TxLease):
        raise PttRefused(_("{what} is not a TX lease").format(what=type(lease).__name__))
    why = lease.refusal()
    if why:
        raise PttRefused(why)
