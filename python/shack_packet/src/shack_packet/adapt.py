"""Link adaptation for the fast modes: each direction of a connection
runs at the fastest mode its receiver can currently decode, stepping down
on evidence and back up after a run of clean windows. OpenHamShack to
OpenHamShack only, on SDR links that receive several modes at once
(radios.pluto with `modes`); asked for by Brian on 18/10/2026 after the
first 4fsk76800 transfer on the air sat in T1: the nano's weak direction
lost its acknowledgements.

**The receiver decides.** Whoever receives a direction knows best how it
is doing - which frames went missing, how loud the other station is - so
each station runs an `RxAdapter` per connection that picks the mode it
wants the other to send to it, and tells it in-band: an XID command
carrying OpenHamShack's private parameter (xid.PI_OHS_MODES: our top mode
and the mode we ask for), sent at connect (the 2.2 XID after UA) and again
whenever the request changes. The XID response the other station must
send carries its own request for the other direction, so one exchange
updates both. An unanswered request is sent again (ANNOUNCE_RETRY_S). The
sender only ever sends I frames at the mode last asked for: one authority
per direction, so the two ends never disagree for long about what to send
and what to listen for.

Rejected: the sender stepping down by itself after T1 expiries. Then two
stations decide one direction, and a receiver has to guess which mode
the next window comes in. The sender's evidence reaches the receiver
anyway: a T1 expiry is a poll (RR with P), and a poll arriving when no
new I frame came since the last one means the I frames are not getting
through - which the receiver acts on. Also rejected: a robust preamble
naming each over's mode. It costs every over its length, and the
receiver still needs every candidate demodulator running to hear it.

**The control path never adapts.** Everything that is not an I frame -
RR, REJ, SREJ, XID, SABM(E), UA, DISC, DM, UI - goes at the ladder's
floor, the most robust mode both ends listen to all the time (g3ruh9600
by default). That is what lost the transfer on 18/10: 4fsk76800 RRs from
the weak end. A transmission may carry both: its control frames first at
the floor, then its I frames at the data mode, each part with its own
preamble (channel.py). An acknowledgement at 9600 baud costs ~30 ms of
airtime a window cycle.

**The evidence** (`RxAdapter`), checked when this station acknowledges a
burst of I frames (and so knows what it got and what it did not):

- *clean*: a burst with no more than LOSS_CLEAN of it missing;
- *loss*: a quarter or more of a burst missing (SREJ's gaps, or a REJ):
  one step down;
- *a poll with nothing new since we last acknowledged*: one step down
  (acknowledgements go at the floor, so a poll is almost always data
  lost, not an acknowledgement lost);
- *level*: the received C/N0 (from the receiver's channel power over its
  floor, every frame heard from this station, control frames included;
  falling at once, rising slowly) below what the mode needs (THRESHOLD_DBHZ - DOWN_MARGIN_DB):
  straight down to the fastest mode it does support;
- *up*: after a clean burst (`up_level_needed`, 1 to start), straight
  up to the fastest mode whose threshold the level clears by UP_MARGIN_DB
  (so 4 dB of hysteresis against the step down); with no level to go by,
  one step after UP_WINDOWS clean bursts. An up-step followed by a step
  down for loss or a poll (not for level: that is a fade, which the level
  tracks by itself) within PROBE_BURSTS doubles both counts (to UP_LEVEL_MAX and
  UP_WINDOWS_MAX): what keeps a marginal link from flapping. A
  direction with no I frames for IDLE_S follows the level up too.
- *short bursts at slow modes*: on an adaptive link one transmission
  carries at most OVER_S of I frames, so at 9600 baud the evidence (and
  the chance to step back up) comes every couple of seconds, not after a
  window that fills max_tx_s.

Frame CRC failures are not counted on their own. On the fast modes a
receiver's framer finds "frames" with a bad FCS in plain noise between
overs, so the count says little; a frame that failed its FCS during a
burst is a missing N(S), which the loss rule already counts.

**Listening**: the receiver demodulates the floor, the mode it asked
for, the one below it (so a step down is heard before it is announced),
and the mode it asked for before, until a frame arrives at the new one
(or LISTEN_GRACE_S passes): at most four of the five ladder modes, which
the Pluto's receive keeps up with on both test hosts (README).

**The ladder** is ordered by THRESHOLD_DBHZ, the C/N0 each mode needs for
nearly every frame to decode (the IQ loopback's ~90 % point: 12 dB CNR in
the occupied bandwidth for the two-level modes, 18 dB for 4-FSK). By that
measure 4fsk38400 (66.5 dB-Hz) is worse than gfsk76800 (61.2 dB-Hz) at the
same bit rate, so the default ladder leaves it out: 4fsk76800, gfsk76800,
g3ruh38400, g3ruh19200, g3ruh9600. gfsk153600 (64.2 dB-Hz) is more
robust than 4fsk76800 but too wide for 23 cm; a ladder topped by it
(13 cm) goes gfsk153600, gfsk76800, ...
"""

from __future__ import annotations

import collections
import math

from . import modem
from .errors import PacketError

#: CNR (dB, in the mode's occupied bandwidth) for ~90 % of frames
#: (fsk_bench loopback, 18/10/2026).
REQUIRED_CNR_DB = {1: 12.0, 2: 18.0}
DEFAULT_LADDER = ("4fsk76800", "gfsk76800", "g3ruh38400", "g3ruh19200", "g3ruh9600")
DEFAULT_TOP = "4fsk76800"
DEFAULT_FLOOR = "g3ruh9600"
DOWN_MARGIN_DB = 1.0
UP_MARGIN_DB = 3.0
UP_WINDOWS = 4
UP_WINDOWS_MAX = 64
#: Clean bursts before a step up the level supports: 1, doubled by each
#: probe that fails (to UP_LEVEL_MAX).
UP_LEVEL_MAX = 16
#: The longest stretch of I frames in one transmission on an adaptive
#: link: a window at the top mode (63 x 512 bytes is 1.7 s at 153.6
#: kbit/s), and at a slow mode short enough that the evidence for coming
#: back up arrives every couple of seconds rather than every max_tx_s.
OVER_S = 2.0
PROBE_BURSTS = 3
LOSS_STEP_DOWN = 0.25
#: A burst counts as clean with no more than this much of it missing: one
#: frame in a 63-frame window resent by SREJ costs under 2 %, and a link
#: that fades now and then never had a burst with nothing missing.
LOSS_CLEAN = 0.1
#: A direction with no I frames for this long follows the level up as
#: well as down: there is no burst to judge it by, and the mode it is
#: left at is the one the next data starts at.
IDLE_S = 5.0
LISTEN_GRACE_S = 10.0
ANNOUNCE_RETRY_S = 3.0
ANNOUNCE_TRIES = 6
#: A poll counts as evidence only while data was moving this recently: a
#: T3 poll on an idle link says nothing about the data mode.
POLL_EVIDENCE_S = 60.0
#: The level estimate's weight for each new measurement.
CN0_WEIGHT = 0.3


def threshold_dbhz(mode) -> float:
    """The C/N0 a mode needs (module docstring)."""
    m = modem.mode(mode)
    return REQUIRED_CNR_DB[m.bits] + 10.0 * math.log10(m.occupied_hz)


def ladder(top: str = DEFAULT_TOP, floor: str = DEFAULT_FLOOR) -> tuple[str, ...]:
    """The modes from `top` down to `floor`, fastest first, each more robust
    than the one before (module docstring)."""
    top, floor = modem.mode(top).name, modem.mode(floor).name
    if top not in modem.CODES or floor not in modem.CODES:
        raise PacketError("link adaptation runs on the fast modes only")
    if threshold_dbhz(floor) > threshold_dbhz(top):
        raise PacketError(f"the floor {floor} needs more signal than the top {top}")
    out = [top]
    for name in DEFAULT_LADDER:
        if threshold_dbhz(floor) <= threshold_dbhz(name) < threshold_dbhz(out[-1]):
            out.append(name)
    if out[-1] != floor:
        out.append(floor)
    return tuple(out)


def cn0_from_level(level_db: float | None, noise_bw_hz: float) -> float | None:
    """C/N0 (dB-Hz) from a channel's power over its noise floor, (C+N)/N
    in dB, measured through a filter of noise bandwidth `noise_bw_hz`."""
    if level_db is None:
        return None
    ratio = 10.0 ** (level_db / 10.0) - 1.0
    if ratio <= 0.0:
        return None
    return 10.0 * math.log10(ratio) + 10.0 * math.log10(noise_bw_hz)


class RxAdapter:
    """One direction's receiving end: which mode to ask the other station
    to send to us (module docstring). `now` is the engine's clock."""

    def __init__(self, modes: tuple[str, ...], now: float = 0.0):
        self.modes = tuple(modes)
        self.floor = self.modes[-1]
        self.idx = 0
        self.prev: str | None = None
        self.changed_at = now
        self.heard_new = True             # an I frame at the current request since it changed
        self.cn0: float | None = None
        self.clean = 0
        self.up_needed = UP_WINDOWS
        self.up_level_needed = 1
        self.since_up: int | None = None
        self.data_since_ack = False
        self.last_data = -1e9
        #: The mode the burst being judged came in.
        self.burst_mode: str | None = None
        self.announce_pending = True
        self.stats = collections.Counter()
        self.history: list[tuple[float, str, str]] = []

    @property
    def request(self) -> str:
        return self.modes[self.idx]

    def listening(self, now: float) -> set[str]:
        out = {self.floor, self.request}
        if self.idx + 1 < len(self.modes):
            out.add(self.modes[self.idx + 1])
        if self.prev is not None and not self.heard_new and now - self.changed_at < LISTEN_GRACE_S:
            out.add(self.prev)
        return out

    # -- evidence ---------------------------------------------------------------------------

    def heard(self, mode: str | None, cn0: float | None, now: float, data: bool = False) -> None:
        """Any frame from the other station; `data`: an I frame in sequence.
        The level falls at once and rises slowly: a fade shows in the very
        next frame heard (often a poll at the floor, the only thing still
        getting through), and the mode follows it down there and then."""
        if cn0 is not None:
            if self.cn0 is None or cn0 < self.cn0:
                self.cn0 = cn0
            else:
                self.cn0 = (1 - CN0_WEIGHT) * self.cn0 + CN0_WEIGHT * cn0
            best = self._best_by_level()
            if best is not None and best > self.idx:
                self._step(best, "level", now)
            elif not data and now - self.last_data > IDLE_S:
                target = self._up_by_level()
                if target is not None:
                    self._step(target, "idle-level", now)
        if data:
            self.data_since_ack = True
            self.last_data = now
            self.burst_mode = mode
            if mode == self.request:
                self.heard_new = True

    def poll(self, now: float) -> None:
        """The other station's T1 expired and it polled us: if nothing new
        came since we last acknowledged, its last window did not get here."""
        if not self.data_since_ack and now - self.last_data < POLL_EVIDENCE_S:
            self._step(self.idx + 1, "poll", now)
        self.data_since_ack = False

    def burst(self, received: int, missing: int, now: float, confirmed: bool = True) -> None:
        """We are acknowledging: `received` I frames in sequence (or held)
        since the last acknowledgement, `missing` still to come.
        `confirmed`: the other station has answered our latest request, so
        the burst was sent at it; until then its losses are the old mode's."""
        self.data_since_ack = False
        best = self._best_by_level()
        if best is not None and best > self.idx:
            self._step(best, "level", now)
            return
        total = received + missing
        stale = not confirmed or (self.burst_mode is not None and self.burst_mode != self.request)
        self.burst_mode = None
        if stale:
            # Sent at a mode we have already left (the level stepped us
            # down while it was on the air): it says nothing about this
            # one, except that a level well clear of a faster mode with
            # nothing missing may take us straight back up.
            target = self._up_by_level() if not missing and received else None
            if target is not None and self.up_level_needed <= 1:
                self._step(target, "level-up", now)
                self.since_up = 0
            return
        if missing and missing >= LOSS_STEP_DOWN * total:
            self._step(self.idx + 1, "loss", now)
            return
        if not received or missing > LOSS_CLEAN * total:
            self.clean = 0
            return
        self.clean += 1
        if self.since_up is not None:
            self.since_up += 1
            if self.since_up >= PROBE_BURSTS:
                # The step up held: relax the wait again, or a link that
                # fades now and then (each fade looks like a failed probe)
                # climbs back slower after every one.
                self.since_up = None
                self.up_needed = max(UP_WINDOWS, self.up_needed // 2)
                self.up_level_needed = max(1, self.up_level_needed // 2)
        if self.idx == 0:
            return
        if self.cn0 is not None:
            # Up as far as the level clears by UP_MARGIN_DB, once a clean
            # burst or more (up_level_needed) says the present mode works.
            target = self._up_by_level()
            if target is not None and self.clean >= self.up_level_needed:
                self._step(target, "clean", now)
                self.since_up = 0
        elif self.clean >= self.up_needed:
            # No level to go by (a link that cannot measure one): one step
            # at a time, after a run of clean bursts.
            self._step(self.idx - 1, "clean", now)
            self.since_up = 0

    def _up_by_level(self) -> int | None:
        """The fastest mode above the present one whose threshold the level
        clears by UP_MARGIN_DB, or None."""
        if self.cn0 is None:
            return None
        return next((i for i, name in enumerate(self.modes[:self.idx])
                     if self.cn0 >= threshold_dbhz(name) + UP_MARGIN_DB), None)

    def _best_by_level(self) -> int | None:
        if self.cn0 is None:
            return None
        for i, name in enumerate(self.modes):
            if self.cn0 >= threshold_dbhz(name) - DOWN_MARGIN_DB:
                return i
        return len(self.modes) - 1

    def _step(self, idx: int, why: str, now: float) -> None:
        idx = max(0, min(len(self.modes) - 1, idx))
        if idx == self.idx:
            return
        down = idx > self.idx
        if down and self.since_up is not None and why in ("loss", "poll"):
            # The last step up did not hold although the level said it
            # would: wait longer before the next. (A step down for level
            # is a fade, which the level follows by itself.)
            self.up_needed = min(UP_WINDOWS_MAX, self.up_needed * 2)
            self.up_level_needed = min(UP_LEVEL_MAX, self.up_level_needed * 2)
            self.since_up = None
        self.stats["down" if down else "up"] += 1
        self.stats[f"{'down' if down else 'up'}_{why}"] += 1
        self.prev = self.request
        self.idx = idx
        self.changed_at = now
        self.heard_new = False
        self.clean = 0
        self.announce_pending = True
        self.history.append((now, self.request, why))
        if len(self.history) > 200:
            del self.history[:100]
