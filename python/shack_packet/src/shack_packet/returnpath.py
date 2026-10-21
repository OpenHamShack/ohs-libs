"""A return path: one connection's reverse direction carried by something
other than the radio, for a link test between two stations that can each
only do one thing on the air.

Asked for by Brian on 18/10/2026 for the first on-air test with GM0OTB:
each station has a Pluto and one antenna and no T/R switch, so each run is
one RF direction - the sender's antenna on its TX port, the receiver's on
its RX port. Connected mode needs the receiver's answers (UA, RR, REJ,
SREJ, XID, DM...) to reach the sender, and they cannot go by radio. A
`ReturnPath` carries them (in practice through the OpenHamShack relay on
the controller box, linktest.py) and the sender's engine takes what
arrives on it as if it had been heard - marked as not heard.

**One per remote station, attached by name, never by default.** An engine
has none until the app attaches one (`Engine.attach_return_path`) for one
remote callsign (call and SSID). Then, for that station only:

- *outbound* (the RF receiver's end): every frame this station would send
  to that station goes on the return path instead of the radio. The
  channel never sees it, so the radio is never keyed for it; on the test
  the receiving station also has transmit off, so nothing could key it
  anyway. All of a connection's frames go, I frames included: an RF
  receiver has no antenna on its TX port, so an I frame queued for the
  radio would only be refused. Acknowledgements wait until the receiver
  has heard no carrier for RETURN_SETTLE_S, as a half-duplex station waits
  for the sender's over to end: sent mid-burst they would acknowledge one
  frame at a time and the adapter would judge single-frame bursts.
- *inbound* (both ends): a frame arriving on it is acted on only if it is
  from that station to one of ours; anything else is counted and dropped.
  It goes through the same path as a frame heard on the air, with no level
  and no mode, so link adaptation never takes an internet frame as
  evidence about the radio.

The sender's own transmissions are untouched: its I frames, polls and
SABM still go by radio.

**Never silent.** Attaching, detaching and losing a return path are engine
events ("return-path": on/off/lost) and log warnings; every frame through
it is counted in `Engine.stats` (rp_sent, rp_heard, rp_refused,
rp_dropped) and reported to the engine's taps with `via` set to the
path's name ("internet"), so a monitor and the telemetry say which frames
were not on the air.

**Losing it fails safe.** When the path goes down (`ReturnPathSink.
return_lost`: the relay closed, a heartbeat was missed, the other station
left) every connection to that station is dropped at once *without
transmitting anything* (DataLink.drop), and frames to it still queued for
the radio are discarded: the sender stops after the over in progress, if
any, and the radio stays quiet. `connect()` to a station whose return path
is down is refused.

A return path is anything with this interface; `LoopReturnPath` joins two
engines in one process (the tests), linktest.RelayReturnPath goes through
the relay.
"""

from __future__ import annotations

import collections
import logging
import random
from typing import Protocol

from .ax25 import Address

log = logging.getLogger(__name__)

#: Seconds without carrier before an RF receiver answers by return path
#: (module docstring). Twice the engine's 20 ms receive block, so a DCD
#: that flickers between two frames of one burst does not count as the end.
RETURN_SETTLE_S = 0.05

#: Why a return path went down or a link through it was dropped, as keys
#: the app translates (as datalink.REASONS).
REASONS = {
    "return-path-lost": "the internet return path was lost; the link was dropped without transmitting",
    "return-path-off": "the internet return path was switched off; the link was dropped without transmitting",
    "relay-closed": "the relay closed the connection",
    "relay-silent": "nothing from the relay for too long",
    "peer-left": "the other station left the link test",
    "peer-silent": "nothing from the other station for too long",
    "unreachable": "the relay could not be reached",
    "refused": "the relay refused to join this session",
}


class ReturnPathSink(Protocol):
    """What a return path calls back into (the Engine). Either may be called
    from the path's own thread; neither blocks for long."""

    def return_frame(self, path: ReturnPath, frame: bytes) -> None: ...

    def return_lost(self, path: ReturnPath, reason: str) -> None: ...


class ReturnPath:
    """The interface. `remote` is the one station it carries frames to and
    from; `outbound` whether this station's frames to it go this way (the
    RF receiver's end) or only come back this way (the sender's). `name`
    is what monitors and logs call it. `up` is False until it can carry a
    frame both ways, and again once lost."""

    name = "internet"

    def __init__(self, remote, *, outbound: bool):
        self.remote = remote if isinstance(remote, Address) else Address.from_text(str(remote), flag=False)
        self.outbound = bool(outbound)
        self.sink: ReturnPathSink | None = None

    @property
    def up(self) -> bool:
        raise NotImplementedError

    def bind(self, sink: ReturnPathSink) -> None:
        """The engine attaching itself."""
        self.sink = sink

    def send(self, frame: bytes) -> None:
        """One frame (without FCS) to the other station. Called under the
        engine's lock: queue it, never block."""
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "remote": str(self.remote), "outbound": self.outbound, "up": self.up}

    # -- for implementations -------------------------------------------------------

    def _deliver(self, frame: bytes) -> None:
        if self.sink is not None:
            self.sink.return_frame(self, bytes(frame))

    def _lost(self, reason: str) -> None:
        if self.sink is not None:
            self.sink.return_lost(self, reason)


class LoopReturnPath(ReturnPath):
    """Two ends of a return path in one process, for the tests:
    `a, b = LoopReturnPath.pair("GM4BRI", "GM0OTB", latency_s=0.05,
    clock=world.clock)`; `a` goes on GM0OTB's engine (outbound, it is the
    RF receiver) and `b` on GM4BRI's. Each end is an engine service, so
    frames arrive on the engine's own poll at simulated time; `cut()` takes
    it down at both ends.

    `jitter_s` adds up to that much to each frame's latency and `loss`
    drops that fraction of frames (seeded by `seed`), for the web relay
    (weblink.py), where a frame takes 0.3-1.5 s and a batch can be
    refused. Order is kept, as the relay keeps it: a frame never overtakes
    the one before it."""

    def __init__(self, remote, *, outbound: bool, latency_s: float, clock, jitter_s: float = 0.0,
                 loss: float = 0.0, seed: int = 1):
        super().__init__(remote, outbound=outbound)
        self.latency_s = latency_s
        self.jitter_s = jitter_s
        self.loss = loss
        self._rng = random.Random(seed)
        self.lost_frames = 0
        self.clock = clock
        self.peer: LoopReturnPath | None = None
        self._up = True
        self._inbox: collections.deque[tuple[float, bytes]] = collections.deque()
        self.sent: list[bytes] = []

    @classmethod
    def pair(cls, sender, receiver, *, latency_s: float = 0.05, clock, jitter_s: float = 0.0, loss: float = 0.0,
             seed: int = 1):
        """(the receiver's end, the sender's end)."""
        rx = cls(sender, outbound=True, latency_s=latency_s, clock=clock, jitter_s=jitter_s, loss=loss, seed=seed)
        tx = cls(receiver, outbound=False, latency_s=latency_s, clock=clock, jitter_s=jitter_s, loss=loss,
                 seed=seed + 1)
        rx.peer, tx.peer = tx, rx
        return rx, tx

    @property
    def up(self) -> bool:
        return self._up

    def send(self, frame: bytes) -> None:
        if not self._up:
            return
        self.sent.append(bytes(frame))
        if self.loss and self._rng.random() < self.loss:
            self.lost_frames += 1
            return
        at = self.clock() + self.latency_s + (self._rng.uniform(0, self.jitter_s) if self.jitter_s else 0.0)
        inbox = self.peer._inbox
        if inbox and inbox[-1][0] > at:
            at = inbox[-1][0]
        inbox.append((at, bytes(frame)))

    def cut(self, reason: str = "relay-closed") -> None:
        for end in (self, self.peer):
            if end._up:
                end._up = False
                end._inbox.clear()
                end._lost(reason)

    # An engine service (engine.services): poll(now), next_deadline().
    def poll(self, now: float) -> None:
        while self._inbox and self._inbox[0][0] <= now + 1e-9:
            _t, frame = self._inbox.popleft()
            self._deliver(frame)

    def next_deadline(self) -> float | None:
        return self._inbox[0][0] if self._inbox else None
