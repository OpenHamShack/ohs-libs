"""The AX.25 2.2 connected-mode data link: one `DataLink` per pair of
stations, following the specification's SDL (AX.25 2.2, July 1998, with
the 2017 revised edition's corrections), state for state.

States: DISCONNECTED, AWAITING_CONNECTION (SABM sent), AWAITING_V22 (SABME
sent), AWAITING_RELEASE (DISC sent), CONNECTED and TIMER_RECOVERY. The
variables keep the specification's names: V(S) next to send, V(A) oldest
unacknowledged, V(R) next expected, RC the retry count, SRT the smoothed
round trip, T1V the T1 value.

**Choices the specification leaves open, and why:**

- *REJ, not SREJ.* A received frame out of sequence is discarded and REJ
  asks for everything from V(R) again; nothing out of sequence is held.
  That is what nearly every station on the air does, it needs no
  reassembly buffer and no SREJ negotiation, and at 1200 baud half duplex
  the difference is small: a lost frame costs the frames after it in the
  same transmission either way, because the sender only hears the REJ after
  its whole burst. XID advertises REJ only. An SREJ received anyway (a peer
  that did not ask) is answered by resending the one frame it names.
- *SREJ between two stations that both offer it* (LinkConfig.srej, XID).
  At 38 400 baud and up a transmission is a window of 32-63 frames, and
  with REJ one frame lost early in it costs every frame after it: on the
  simulated channel (fsk_bench throughput, 153 600 baud, k 63, a bit error
  rate of 1e-5) ten frames lost cost 230 retransmissions. With SREJ the
  receiver keeps frames that arrive out of sequence (at most the window,
  which is why the window must be no more than half the modulus) and,
  with its next RR, names each missing one in an SREJ of its own (2.2's
  multi-SREJ, one S frame each); the sender resends just those. An SREJ
  acknowledges nothing; the RR before it does. A poll is still answered
  with RR alone, and the sender in timer recovery still goes back to
  N(R): the receiver drops what it already holds.
- *Link adaptation* (LinkConfig.adapt, adapt.py): the receiver of each
  direction asks for a mode in XID (a private parameter, at connect and
  whenever it changes, sent again until answered); I frames go at the
  mode asked for, S and U frames at the ladder's floor. The evidence it
  steps on is gathered here: each frame heard (its mode and level), each
  poll, and each burst acknowledged (frames received, frames missing).
- *FRMR is received, never sent.* 2.2 withdrew sending it (§4.3.3.9): a
  station that receives something it cannot accept re-establishes the link
  instead, and so does this one, on an FRMR from an older peer too. A frame
  whose control field names nothing AX.25 defines never reaches here
  (ax25.parse returns None) and is dropped like a bad FCS.
- *T1 adapts to the measured round trip.* T1 runs from when the last
  frame that needs answering has left the air (channel.Outgoing.on_sent),
  not from when it was queued, so neither the wait for a clear channel nor
  our own airtime is counted as the peer being slow. It is not paused while
  someone else holds the channel, as direwolf pauses it: measured over
  eight seeds on the simulated channel, pausing cost 10-20 % of the
  throughput, because recovering a lost frame then waited longer. An acknowledgement that arrives with no
  retry in between (Karn's rule) updates SRT = 7/8 SRT + 1/8 RTT and T1V =
  2 SRT; each expiry doubles T1V, up to T1_MAX. The 1998 SDL's "T1V = RC/4
  + 2 SRT" barely backs off at all, which on a congested 1200-baud channel
  turns retries into more congestion; doubling is the 2017 edition's
  direction and TCP's. The first T1 is FRACK (3 s) x (2 x digipeaters + 1),
  as direwolf does: each digipeater repeats the frame and the answer.
- *SABME falls back to SABM.* AWAITING_V22 retries SABME V22_TRIES times,
  then tries SABM as a 2.0 station would, because a 2.0 TNC ignores SABME
  rather than answering it. An FRMR to SABME falls back at once; a DM is a
  refusal, as the specification says, not a reason to try again.
- *XID*: answered whenever received, with what this station can take; a
  2.2 link this station opened sends one XID command after UA, as direwolf
  does (`xid`); a peer that never answers costs nothing.
- *DL-DISCONNECT discards the I queue* (the SDL), so `close()` waits for
  what was sent to be acknowledged first and `abort()` is the SDL's.

The engine routes frames here already parsed at the link's modulus, and
asks each link for frames when it is about to transmit (`frames_for_tx`),
so an acknowledgement goes with the newest N(R) and I frames go when the
window allows.
"""

from __future__ import annotations

import collections
import enum
import logging
from dataclasses import dataclass
from typing import Callable

from . import xid
from .ax25 import PID_NONE, Address, Control, Frame, FrameType
from .channel import Outgoing, frame_airtime_s

log = logging.getLogger(__name__)


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    AWAITING_CONNECTION = "awaiting-connection"
    AWAITING_V22 = "awaiting-v22-connection"
    AWAITING_RELEASE = "awaiting-release"
    CONNECTED = "connected"
    TIMER_RECOVERY = "timer-recovery"


#: Why a link changed state, as keys the app translates (as aprs.PROBLEMS).
#: The letters in brackets are the specification's DL-ERROR codes.
REASONS = {
    "connected": "the link is up",
    "connected-by-peer": "a station connected to us",
    "refused": "the other station refused the connection (DM)",
    "timeout": "no answer after N2 tries (T1 expired)",
    "closed": "we disconnected",
    "closed-by-peer": "the other station disconnected (DISC)",
    "dm-received": "the other station said it is not connected (DM) [E]",
    "reset-by-peer": "the other station reset the link (SABM while connected) [F]; data may be lost",
    "reset-frmr": "the other station rejected a frame (FRMR) [K]; the link was re-established",
    "reset-nr": "the other station acknowledged a frame never sent [J]; the link was re-established",
    "reset-ua": "an unexpected UA [C]; the link was re-established",
    "reset-n1": "an information field longer than N1 [O]; the link was re-established",
    "peer-busy": "the other station cannot take more data now (RNR)",
    "peer-ready": "the other station can take data again",
    "fallback-v20": "the other station does not do AX.25 2.2; connecting as 2.0",
    # returnpath.py: a link test's internet return path.
    "return-path-lost": "the internet return path was lost; the link was dropped without transmitting",
    "return-path-off": "the internet return path was switched off; the link was dropped without transmitting",
}

#: Seconds. FRACK is direwolf's and most TNCs' default first T1.
FRACK = 3.0
T1_MIN = 1.0
T1_MAX = 60.0
T3 = 300.0
N2 = 10
V22_TRIES = 3


@dataclass
class LinkConfig:
    """What this station does on a link. k is the window (frames
    outstanding), n1 the longest information field sent, n1_rx the longest
    accepted. Defaults are direwolf's (MAXFRAME 4, EMAXFRAME 8, PACLEN
    256, RETRY 10, FRACK 3)."""
    modulo: int = 8
    k: int | None = None
    n1: int = 256
    n1_rx: int = 2048
    n2: int = N2
    frack_s: float = FRACK
    t1_min_s: float = T1_MIN
    t1_max_s: float = T1_MAX
    t3_s: float = T3
    xid: bool = True
    #: False to behave as an AX.25 2.0 station: SABME is ignored (as a 2.0
    #: TNC ignores it) and connections are modulo 8. For a peer known to
    #: half-implement 2.2, as direwolf's V20 option.
    v22: bool = True
    #: Unread received bytes above which this station says RNR (own
    #: receiver busy), and below which (half of it) RR again.
    rx_limit: int = 16 * 1024
    #: Selective reject (module docstring), offered in XID and used only
    #: when the peer offers it too and the window is at most half the
    #: modulus. Off by default: what other stations do with SREJ varies,
    #: and REJ is what this link is proven with against direwolf. Two OHS
    #: stations on the fast modes turn it on.
    srej: bool = False
    #: Link adaptation (adapt.py): the ladder of fast modes, fastest first,
    #: or None. Both stations must offer it (XID); then I frames go at the
    #: mode the other station asks for and everything else at the ladder's
    #: floor.
    adapt: tuple[str, ...] | None = None
    #: The mode on that ladder our receive side starts by asking for, when
    #: something already says what this path carries (a path test,
    #: pathtest.Tally.best_mode); None starts at the top.
    adapt_start: str | None = None

    @property
    def window(self) -> int:
        if self.k is not None:
            return self.k
        return 4 if self.modulo == 8 else 8


class _Timer:
    def __init__(self):
        self.deadline: float | None = None
        self.started: float = 0.0
        self.token = 0

    @property
    def running(self) -> bool:
        return self.deadline is not None

    def start(self, now: float, value: float) -> int:
        self.deadline = now + value
        self.started = now
        self.token += 1
        return self.token

    def stop(self) -> None:
        self.deadline = None

    def expired(self, now: float) -> bool:
        return self.deadline is not None and now + 1e-9 >= self.deadline


class DataLink:
    """One AX.25 connection. `send` is the engine's: (Outgoing) -> None,
    queuing a frame for the channel; `events(kind, reason)` reports state
    changes; `deliver(pid, data)` hands up received I-frame data."""

    def __init__(self, local: Address, remote: Address, via, config: LinkConfig, *,
                 send: Callable[[Outgoing], None], now: Callable[[], float]):
        self.local = Address(local.call, local.ssid)
        self.remote = Address(remote.call, remote.ssid)
        self.via = [Address(h.call, h.ssid) for h in via]
        self.config = config
        self._send = send
        self._now = now
        #: The channel's baud (modem.py), for the airtime a frame costs
        #: against a transmission's budget; the engine sets it.
        self.baud = 1200
        self.events: Callable[[str, str], None] = lambda kind, reason: None
        self.deliver: Callable[[int, bytes], None] = lambda pid, data: None
        self.state = State.DISCONNECTED
        self.modulo = config.modulo
        self.k = config.window
        self.n1 = config.n1
        self.n2 = config.n2
        self.vs = self.va = self.vr = 0
        self.rc = 0
        self.srt = config.frack_s * (2 * len(self.via) + 1) / 2.0
        self.t1v = 2.0 * self.srt
        self.t1 = _Timer()
        self.t3 = _Timer()
        self.layer3_initiated = False
        self.peer_busy = False
        self.own_busy = False
        self.reject_exception = False
        self.ack_pending = False
        #: SREJ in use on this link (both offered it; LinkConfig.srej).
        self.srej = False
        #: Frames received out of sequence while SREJ is in use: N(S) ->
        #: (pid, info).
        self._held: dict[int, tuple[int, bytes]] = {}
        self._enquiry_due = False
        #: Link adaptation (adapt.py): our receive side's adapter, the mode
        #: we send I frames at, and when to (re)send our request.
        self.adapter = None
        self.tx_mode: str | None = None
        self.peer_adapts = False
        self._announce_due: float | None = None
        self._announce_tries = 0
        self._burst_rx = 0
        self._burst_lost = 0
        self._asked: set[int] = set()
        self._rx_mode: str | None = None
        self._queue: collections.deque[tuple[int, bytes]] = collections.deque()
        self._sent: dict[int, tuple[int, bytes]] = {}
        self._closing = False
        self._v22_tries = 0
        #: Counters, for the tests and the app's link status.
        self.stats = collections.Counter()

    # -- what the layer above asks for (the DL primitives) -----------------------

    @property
    def connected(self) -> bool:
        return self.state in (State.CONNECTED, State.TIMER_RECOVERY)

    def connect(self) -> None:
        """DL-CONNECT request."""
        self._queue.clear()
        self._sent.clear()
        self.layer3_initiated = True
        self._closing = False
        self.srt = self.config.frack_s * (2 * len(self.via) + 1) / 2.0
        self.t1v = 2.0 * self.srt
        self._v22_tries = 0
        if self.config.modulo == 128:
            self.modulo = 128
            self._establish(State.AWAITING_V22)
        else:
            self.modulo = 8
            self._establish(State.AWAITING_CONNECTION)

    def disconnect(self) -> None:
        """DL-DISCONNECT request, the SDL's: the I queue is discarded."""
        self._closing = False
        if self.state in (State.AWAITING_CONNECTION, State.AWAITING_V22):
            self.t1.stop()
            self._u(FrameType.DM, False, command=False)
            self._to(State.DISCONNECTED, "closed")
        elif self.connected:
            self._queue.clear()
            self._sent.clear()
            self.rc = 0
            self._u(FrameType.DISC, True, needs_t1=True)
            self.t3.stop()
            self.state = State.AWAITING_RELEASE

    def drop(self, reason: str) -> None:
        """Leave the link now and send nothing at all: not DISC, not DM.
        For a link whose other direction has gone (returnpath.py), where
        anything sent could only go out by radio unanswered."""
        self._queue.clear()
        self.ack_pending = False
        if self.state is not State.DISCONNECTED:
            self._to(State.DISCONNECTED, reason)

    def close(self) -> None:
        """Disconnect once everything queued has been acknowledged."""
        if not self.connected:
            self.disconnect()
            return
        self._closing = True
        self._maybe_close()

    def data(self, payload: bytes, pid: int = PID_NONE) -> None:
        """DL-DATA request: one I frame's worth (at most n1 bytes)."""
        if self.state is State.DISCONNECTED or self.state is State.AWAITING_RELEASE:
            return
        if self.state in (State.AWAITING_CONNECTION, State.AWAITING_V22) and not self.layer3_initiated:
            return
        self._queue.append((pid, bytes(payload)))

    def set_busy(self, busy: bool) -> None:
        """DL-FLOW-OFF / DL-FLOW-ON: this station's receiver is (not) busy."""
        if busy == self.own_busy:
            return
        self.own_busy = busy
        if self.connected:
            # 2.2: RNR (or RR) response at once, so the peer stops (or
            # resumes) without waiting for its T1.
            self._s(FrameType.RNR if busy else FrameType.RR, False, command=False)
            self.ack_pending = False

    @property
    def queued(self) -> int:
        """I frames not yet acknowledged or not yet sent."""
        return len(self._queue) + len(self._sent)

    # -- timers --------------------------------------------------------------------

    def next_deadline(self) -> float | None:
        due = [t.deadline for t in (self.t1, self.t3) if t.deadline is not None]
        if self._announce_due is not None and self.connected:
            due.append(self._announce_due)
        return min(due) if due else None

    def poll(self, now: float) -> None:
        if self._announce_due is not None and self.connected and now + 1e-9 >= self._announce_due:
            self._announce(now)
        if self.t1.expired(now):
            self.t1.stop()
            self._t1_expired(now)
        if self.t3.expired(now):
            self.t3.stop()
            if self.state is State.CONNECTED:
                self.rc = 0
                self._enquiry(now)
                self.state = State.TIMER_RECOVERY

    def _t1_expired(self, now: float) -> None:
        self.stats["t1_expired"] += 1
        state = self.state
        if self.rc:
            self.t1v = min(self.config.t1_max_s, self.t1v * 2.0)
        if state in (State.AWAITING_CONNECTION, State.AWAITING_V22):
            if state is State.AWAITING_V22 and self._v22_tries + 1 >= V22_TRIES:
                self.events("info", "fallback-v20")
                self.modulo = 8
                self.rc = 0
                self._establish(State.AWAITING_CONNECTION)
                return
            if self.rc >= self.n2:
                self._queue.clear()
                self._to(State.DISCONNECTED, "timeout")
                return
            self.rc += 1
            self._v22_tries += 1
            kind = FrameType.SABME if state is State.AWAITING_V22 else FrameType.SABM
            self._u(kind, True, needs_t1=True)
        elif state is State.AWAITING_RELEASE:
            if self.rc >= self.n2:
                self._to(State.DISCONNECTED, "closed")
                return
            self.rc += 1
            self._u(FrameType.DISC, True, needs_t1=True)
        elif state is State.CONNECTED:
            self.rc = 1
            self._enquiry(now)
            self.state = State.TIMER_RECOVERY
        elif state is State.TIMER_RECOVERY:
            if self.rc >= self.n2:
                self._queue.clear()
                self._sent.clear()
                self._u(FrameType.DM, False, command=False)
                self.t3.stop()
                self._to(State.DISCONNECTED, "timeout")
                return
            self.rc += 1
            self._enquiry(now)

    # -- frames out ------------------------------------------------------------------

    def _frame(self, control: Control, info: bytes = b"", pid=None, command=True) -> Frame:
        return Frame.build(self.local, self.remote, control, info, via=self.via, pid=pid, command=command)

    def _out(self, frame: Frame, needs_t1: bool = False) -> None:
        on_sent = None
        if needs_t1:
            token = self.t1.start(self._now(), self.t1v)

            def on_sent(t_end, token=token):
                # Restart T1 from the moment the frame left the air, if it is
                # still the timer this frame started.
                if self.t1.running and self.t1.token == token:
                    self.t1.deadline = t_end + self.t1v
                    self.t1.started = t_end
        self.stats["sent_" + frame.type.value] += 1
        self._send(Outgoing(frame.encode(), on_sent))

    def _u(self, kind: FrameType, pf: bool, command: bool = True, needs_t1: bool = False,
           info: bytes = b"") -> None:
        self._out(self._frame(Control(kind, pf), info, command=command), needs_t1)

    def _s(self, kind: FrameType, pf: bool, command: bool) -> None:
        self._out(self._frame(Control(kind, pf, nr=self.vr, modulo=self.modulo), command=command))

    def _enquiry(self, now: float) -> None:
        """transmit_enquiry: RR (or RNR) command with P=1, and T1."""
        kind = FrameType.RNR if self.own_busy else FrameType.RR
        self._out(self._frame(Control(kind, True, nr=self.vr, modulo=self.modulo), command=True), needs_t1=True)
        self.ack_pending = False

    def _enquiry_response(self, final: bool) -> None:
        kind = FrameType.RNR if self.own_busy else FrameType.RR
        self._s(kind, final, command=False)
        self.ack_pending = False

    def _establish(self, state: State) -> None:
        """establish_data_link: clear the exceptions, SABM(E) with P, T1."""
        self._clear_exceptions()
        self.rc = 0
        self.t3.stop()
        kind = FrameType.SABME if state is State.AWAITING_V22 else FrameType.SABM
        self._u(kind, True, needs_t1=True)
        self.state = state

    def _clear_exceptions(self) -> None:
        self.peer_busy = False
        self.reject_exception = False
        self._held.clear()
        self.own_busy = False
        self.ack_pending = False

    def wants_to_send(self) -> bool:
        if not self.connected:
            return False
        if self.adapter is not None and self.adapter.announce_pending:
            return True                     # a new request goes out at the first chance (adapt.py)
        return self.ack_pending or self._window_open() and (self.vs in self._sent or bool(self._queue))

    def _window_open(self) -> bool:
        return not self.peer_busy and (self.vs - self.va) % self.modulo < self._k_now()

    def _k_now(self) -> int:
        """The window: k, or on an adaptive link no more frames than one
        transmission of adapt.OVER_S carries at the present data mode, so
        each window is one over and an answer (and with it a new request)
        comes back after every one. At 9600 baud 63 frames were four
        back-to-back overs, eight seconds in which the other station could
        say nothing while a fade came and went."""
        if self.adapter is None or self.tx_mode is None:
            return self.k
        from . import adapt
        frame_bits = (self.n1 + 20) * 8 * 1.03
        return max(1, min(self.k, int(adapt.OVER_S * self._tx_bitrate() / frame_bits)))

    def frames_for_tx(self, now: float, room: int, budget_s: float = float("inf")) -> list[Outgoing]:
        """The engine is transmitting now: I frames the window allows, else
        the RR an acknowledgement is waiting for. No more than `budget_s`
        of airtime: a frame built is counted outstanding, so one built and
        then left for the next transmission would have T1 running before it
        was ever sent - the case of a modulo-128 window, longer than one
        transmission may be (measured, eight seeds of 50 kB with a beacon
        every 20 s: 777 bit/s without the budget, 801 with)."""
        out: list[Outgoing] = []
        if not self.connected:
            return out
        if self.adapter is not None:
            from . import adapt
            budget_s = min(budget_s, adapt.OVER_S)
        while len(out) < room and self._window_open():
            if self.vs in self._sent:
                pid, payload = self._sent[self.vs]
                retransmission = True
            elif self._queue:
                pid, payload = self._queue[0]
                retransmission = False
            else:
                break
            control = Control(FrameType.I, False, nr=self.vr, ns=self.vs, modulo=self.modulo)
            encoded = self._frame(control, payload, pid=pid, command=True).encode()
            cost = frame_airtime_s(encoded, self._tx_bitrate())
            if cost > budget_s and (out or budget_s < float("inf")):
                break
            budget_s -= cost
            if retransmission:
                self.stats["retransmitted"] += 1
            else:
                self._queue.popleft()
                self._sent[self.vs] = (pid, payload)
            self.vs = (self.vs + 1) % self.modulo
            self.ack_pending = False
            self.stats["sent_I"] += 1
            if not self.t1.running:
                self.t3.stop()
                self.t1.start(now, self.t1v)
            out.append(Outgoing(encoded, self._t1_from_end, mode=self.tx_mode if self.adapter else None))
        if self.adapter is not None and (self._burst_rx or self._burst_lost):
            self._judge_burst(now)
        if self.ack_pending and len(out) < room:
            kind = FrameType.RNR if self.own_busy else FrameType.RR
            frame = self._frame(Control(kind, False, nr=self.vr, modulo=self.modulo), command=False)
            self.stats["sent_" + kind.value] += 1
            out.append(Outgoing(frame.encode()))
            self.ack_pending = False
            for missing in self._missing():
                if len(out) >= room:
                    break
                frame = self._frame(Control(FrameType.SREJ, False, nr=missing, modulo=self.modulo), command=False)
                self.stats["sent_SREJ"] += 1
                out.append(Outgoing(frame.encode()))
        if self.adapter is not None and len(out) < room and (
                self.adapter.announce_pending or (self._announce_due is not None and now + 1e-9 >= self._announce_due)):
            out.append(self._announce(now, queue=False))
        return out

    # -- link adaptation (adapt.py) ----------------------------------------------------

    def _tx_bitrate(self) -> float:
        if self.adapter is not None and self.tx_mode is not None:
            from . import modem
            return modem.mode(self.tx_mode).bitrate
        return self.baud

    def _judge_burst(self, now: float) -> None:
        """We are about to acknowledge: tell the adapter what came. With
        SREJ, missing is what went missing in this burst: a gap asked for
        before (a frame from an earlier window, sent at another mode) is
        not this burst's loss."""
        if self.srej:
            gaps = set(self._missing())
            missing = len(gaps - self._asked)
            self._asked = gaps
        else:
            missing = self._burst_lost
        self.adapter.burst(self._burst_rx, missing, now, confirmed=self._announce_due is None
                           and not self.adapter.announce_pending)
        self._burst_rx = self._burst_lost = 0

    def _announce(self, now: float, queue: bool = True):
        """Our request in an XID command (adapt.py), sent again every
        ANNOUNCE_RETRY_S until an XID response comes back."""
        from . import adapt
        self.adapter.announce_pending = False
        self._announce_tries += 1
        self._announce_due = (now + adapt.ANNOUNCE_RETRY_S if self._announce_tries < adapt.ANNOUNCE_TRIES
                              else None)
        frame = self._frame(Control(FrameType.XID, True), self._xid_params().encode(), command=True)
        self.stats["sent_XID"] += 1
        self.stats["sent_mode_request"] += 1
        if queue:
            self._send(Outgoing(frame.encode()))
            return None
        return Outgoing(frame.encode())

    def _start_adapting(self) -> None:
        from . import adapt
        if not self.config.adapt or self.adapter is not None:
            return
        self.adapter = adapt.RxAdapter(tuple(self.config.adapt), self._now())
        # Our request starts at the top, which is what the other station
        # sends at before it hears us: nothing new to say until it changes.
        # A start from a path test is said at the first chance.
        start = self.config.adapt_start
        if start in self.adapter.modes and start != self.adapter.modes[0]:
            self.adapter.idx = self.adapter.modes.index(start)
        self.adapter.announce_pending = self.adapter.idx != 0
        self.tx_mode = self.adapter.modes[0]

    def adaptation(self) -> dict | None:
        """For an app and the harness: each direction's mode and the steps."""
        if self.adapter is None:
            return None
        a = self.adapter
        return {"tx_mode": self.tx_mode, "rx_mode": a.request, "peer_adapts": self.peer_adapts,
                "rx_steps": dict(a.stats), "tx_changes": self.stats["tx_mode_changes"],
                "cn0_dbhz": None if a.cn0 is None else round(a.cn0, 1)}

    def _missing(self) -> list[int]:
        """With SREJ: each N(S) between V(R) and the furthest frame held
        that has not arrived, oldest first."""
        if not self.srej or not self._held:
            return []
        m = self.modulo
        furthest = max((ns - self.vr) % m for ns in self._held)
        return [(self.vr + i) % m for i in range(furthest) if (self.vr + i) % m not in self._held]

    def _t1_from_end(self, t_end: float) -> None:
        """An I frame has left the air: the peer can answer from now, so T1
        runs from now if it was running from earlier. Only the frame that
        started T1 used to do this, and a window sent in two transmissions
        timed out during the second (100 kB at modulo 128: 750 bit/s, then
        900 with every frame doing it)."""
        if self.t1.running and self.t1.deadline < t_end + self.t1v:
            self.t1.deadline = t_end + self.t1v
            self.t1.started = t_end

    # -- frames in -------------------------------------------------------------------

    def receive(self, frame: Frame, mode: str | None = None, cn0: float | None = None) -> None:
        """A frame from the remote station to us, parsed at self.modulo;
        `mode` it came in and the C/N0 it was heard at, where the radio
        link knows (adaptation's evidence)."""
        self._rx_mode = mode
        if self.adapter is not None:
            self.adapter.heard(mode, cn0, self._now())
        kind = frame.type
        command = frame.command is not False       # 1.x frames count as commands
        pf = frame.control.pf
        self.stats["received_" + kind.value] += 1
        handler = {
            FrameType.SABM: self._rx_sabm, FrameType.SABME: self._rx_sabm,
            FrameType.DISC: self._rx_disc, FrameType.UA: self._rx_ua, FrameType.DM: self._rx_dm,
            FrameType.FRMR: self._rx_frmr, FrameType.XID: self._rx_xid, FrameType.TEST: self._rx_test,
            FrameType.I: self._rx_i, FrameType.RR: self._rx_s, FrameType.RNR: self._rx_s,
            FrameType.REJ: self._rx_s, FrameType.SREJ: self._rx_srej,
        }.get(kind)
        if handler is not None:
            handler(frame, command, pf)

    # U frames

    def _rx_sabm(self, frame: Frame, command: bool, pf: bool) -> None:
        extended = frame.type is FrameType.SABME
        state = self.state
        if state is State.DISCONNECTED:
            # Accepting is the engine's decision, made before the link is
            # created; a link in DISCONNECTED that hears SABM was accepted.
            self._accept(extended, pf, "connected-by-peer")
        elif state in (State.AWAITING_CONNECTION, State.AWAITING_V22):
            # Both ends connecting at once: answer, and let our UA wait.
            self.modulo = 128 if extended else 8
            self._u(FrameType.UA, pf, command=False)
            if state is State.AWAITING_V22 and not extended:
                self.state = State.AWAITING_CONNECTION
        elif state is State.AWAITING_RELEASE:
            self._u(FrameType.DM, pf, command=False)
        else:
            self._u(FrameType.UA, pf, command=False)
            self._clear_exceptions()
            self.modulo = 128 if extended else 8
            if self.vs != self.va or self._queue:
                self._queue.clear()
                self._sent.clear()
            self.t1.stop()
            self.t3.start(self._now(), self.config.t3_s)
            self.vs = self.va = self.vr = 0
            self.state = State.CONNECTED
            self.events("reset", "reset-by-peer")

    def _accept(self, extended: bool, pf: bool, reason: str) -> None:
        self.modulo = 128 if extended else 8
        self.k = self.config.k if self.config.k is not None else (4 if self.modulo == 8 else 8)
        self._u(FrameType.UA, pf, command=False)
        self._clear_exceptions()
        self.vs = self.va = self.vr = 0
        self.rc = 0
        self.t3.start(self._now(), self.config.t3_s)
        self.layer3_initiated = False
        self._to(State.CONNECTED, reason)

    def _rx_disc(self, frame: Frame, command: bool, pf: bool) -> None:
        state = self.state
        if state in (State.DISCONNECTED, State.AWAITING_CONNECTION, State.AWAITING_V22):
            self._u(FrameType.DM, pf, command=False)
        elif state is State.AWAITING_RELEASE:
            self._u(FrameType.UA, pf, command=False)
        else:
            self._queue.clear()
            self._sent.clear()
            self._u(FrameType.UA, pf, command=False)
            self.t1.stop()
            self.t3.stop()
            self._to(State.DISCONNECTED, "closed-by-peer")

    def _rx_ua(self, frame: Frame, command: bool, pf: bool) -> None:
        state = self.state
        if state in (State.AWAITING_CONNECTION, State.AWAITING_V22):
            if not pf:
                return                       # DL-ERROR D: ignore
            self.t1.stop()
            self.t3.start(self._now(), self.config.t3_s)
            self.vs = self.va = self.vr = 0
            self.rc = 0
            self._select_t1()
            self.k = self.config.window if self.modulo == self.config.modulo else (4 if self.modulo == 8 else 8)
            self._to(State.CONNECTED, "connected")
            if self.adapter is not None:
                self._announce(self._now())           # XID with our request, sent again until answered
            elif state is State.AWAITING_V22 and self.config.xid:
                self._u(FrameType.XID, True, info=self._xid_params().encode())
        elif state is State.AWAITING_RELEASE:
            if pf:
                self.t1.stop()
                self._to(State.DISCONNECTED, "closed")
        elif self.connected:
            # DL-ERROR C: re-establish.
            self.layer3_initiated = False
            self._establish(State.AWAITING_V22 if self.modulo == 128 else State.AWAITING_CONNECTION)
            self.events("reset", "reset-ua")

    def _rx_dm(self, frame: Frame, command: bool, pf: bool) -> None:
        state = self.state
        if state in (State.AWAITING_CONNECTION, State.AWAITING_V22):
            if pf:
                self._queue.clear()
                self.t1.stop()
                self._to(State.DISCONNECTED, "refused")
        elif state is State.AWAITING_RELEASE:
            if pf:
                self.t1.stop()
                self._to(State.DISCONNECTED, "closed")
        elif self.connected:
            self._queue.clear()
            self._sent.clear()
            self.t1.stop()
            self.t3.stop()
            self._to(State.DISCONNECTED, "dm-received")

    def _rx_frmr(self, frame: Frame, command: bool, pf: bool) -> None:
        if self.state is State.AWAITING_V22:
            self.events("info", "fallback-v20")
            self.modulo = 8
            self._establish(State.AWAITING_CONNECTION)
        elif self.connected:
            self.layer3_initiated = False
            self._establish(State.AWAITING_V22 if self.modulo == 128 else State.AWAITING_CONNECTION)
            self.events("reset", "reset-frmr")

    def _xid_params(self) -> xid.Parameters:
        # The window offered is the one this link sends with: nothing out of
        # sequence is held here, so any window is safe for us to receive, and
        # a symmetric one keeps a peer from answering a 4-frame burst with a
        # 32-frame one (direwolf offers 32 at modulo 128).
        ohs = None
        if self.adapter is not None:
            from . import modem
            ohs = (modem.CODES[self.adapter.modes[0]], modem.CODES[self.adapter.request])
        return xid.Parameters(ohs_modes=ohs, half_duplex=True, rej=True, srej=self._srej_possible(), modulo=self.modulo,
                              n1_rx=self.config.n1_rx, window_rx=min(self.modulo - 1, self.k),
                              ack_timer_ms=int(self.t1v * 1000), retries=self.n2)

    def _rx_xid(self, frame: Frame, command: bool, pf: bool) -> None:
        theirs = xid.decode(frame.info)
        if theirs is not None:
            self._apply_xid(theirs)
            if not command and theirs.ohs_modes is not None:
                self._announce_due = None           # our request was heard
                self._announce_tries = 0
        if command:
            self._u(FrameType.XID, pf, command=False, info=self._xid_params().encode())

    def _srej_possible(self) -> bool:
        """Selective reject needs the window to be at most half the modulus,
        so a frame held out of sequence can never be mistaken for one a
        whole modulus later."""
        return bool(self.config.srej) and self.k <= self.modulo // 2

    def _apply_xid(self, theirs: xid.Parameters) -> None:
        # Send no more than they can take; never more than we were asked to.
        if theirs.window_rx:
            self.k = max(1, min(self.k, theirs.window_rx, self.modulo - 1))
        if theirs.srej is not None:
            self.srej = bool(theirs.srej) and self._srej_possible()
        if theirs.ohs_modes is not None and self.adapter is not None:
            self._apply_modes(*theirs.ohs_modes)
        if theirs.n1_rx:
            self.n1 = max(1, min(self.n1, theirs.n1_rx))
        if theirs.retries:
            self.n2 = max(self.n2, theirs.retries)
        self.stats["xid_applied"] += 1

    def _apply_modes(self, top_code: int, request_code: int) -> None:
        """The other station's top mode and its request (adapt.py)."""
        from . import adapt, modem
        top, want = modem.BY_CODE.get(top_code), modem.BY_CODE.get(request_code)
        if top is None or want is None:
            return
        self.peer_adapts = True
        ladder = self.adapter.modes
        # Send at what was asked, or the fastest of ours no less robust.
        mode = next((m for m in ladder if adapt.threshold_dbhz(m) <= adapt.threshold_dbhz(want) + 1e-9), ladder[-1])
        if mode != self.tx_mode:
            self.stats["tx_mode_changes"] += 1
            slower = self.tx_mode is not None and adapt.threshold_dbhz(mode) < adapt.threshold_dbhz(self.tx_mode)
            self.tx_mode = mode
            if slower and self.vs != self.va:
                # Asked to slow down: whatever is outstanding went at a mode
                # the other station has given up on, so it goes again now,
                # at the new one, rather than after T1 and a poll (seconds
                # at a time on 18/10's first adaptive runs on the sim).
                self.stats["mode_down_resent"] += (self.vs - self.va) % self.modulo
                self._retransmit_from(self.va)
        # Ask for nothing faster than it can send.
        a = self.adapter
        limit = next((i for i, m in enumerate(ladder) if adapt.threshold_dbhz(m) <= adapt.threshold_dbhz(top) + 1e-9),
                     len(ladder) - 1)
        if a.idx < limit:
            a._step(limit, "peer-top", self._now())

    def _rx_test(self, frame: Frame, command: bool, pf: bool) -> None:
        if command:
            self._u(FrameType.TEST, pf, command=False, info=frame.info)

    # I and S frames

    def _nr_valid(self, nr: int) -> bool:
        """V(A) <= N(R) <= V(S), modulo the modulus - where V(S) is the
        furthest this link has sent, which after a retransmission has moved
        V(S) back is further than V(S): the peer may well have had those
        frames and only its acknowledgement was lost."""
        m = self.modulo
        sent = max((self.vs - self.va) % m, len(self._sent))
        return (nr - self.va) % m <= sent

    def _ack_to(self, nr: int) -> None:
        """V(A) <- N(R), forgetting what it acknowledges."""
        m = self.modulo
        if (nr - self.va) % m > (self.vs - self.va) % m:
            # Acknowledged past a V(S) a retransmission moved back.
            self.vs = nr
        while self.va != nr:
            self._sent.pop(self.va, None)
            self.va = (self.va + 1) % m
        self._maybe_close()

    def _check_i_frame_acked(self, nr: int) -> None:
        now = self._now()
        if self.peer_busy:
            self._ack_to(nr)
            if not self.t1.running:
                self.t1.start(now, self.t1v)
        elif nr == self.vs:
            self._ack_to(nr)
            self._stop_t1_measured(now)
            self.t3.start(now, self.config.t3_s)
        elif nr != self.va:
            self._ack_to(nr)
            self.t1.start(now, self.t1v)

    def _stop_t1_measured(self, now: float) -> None:
        """Everything outstanding is answered: stop T1, learn the round trip
        if nothing was retried (Karn's rule), and forget any back-off - as
        TCP does, the doubling is for one loss, and keeping it after the
        link has recovered made a 15 %-loss transfer crawl at the 60 s cap
        (measured: 10 kB in 6000 s at modulo 128, against 100 kB in 2600 s
        with the back-off forgotten)."""
        # Only a round trip that ends after its reference counts. T1's
        # reference moves to the scheduled end of each I frame as it is
        # handed to the channel (_t1_from_end), so with the answers coming
        # back slowly (the web relay, weblink.py: 0.3-1.5 s) an
        # acknowledgement can arrive while the next over is still queued and
        # the reference lies seconds ahead. Taken as a round trip of zero
        # (it was clamped), it dragged SRT to 0.67 s and T1 below the return
        # trip, and 37 % of a 20 kB transfer at 1.5 s was sent twice
        # (test_returnpath's web relay test).
        if self.t1.running and self.rc == 0 and now > self.t1.started:
            rtt = now - self.t1.started
            self.srt = 7.0 / 8.0 * self.srt + rtt / 8.0
        self._select_t1()
        self.t1.stop()

    def _select_t1(self) -> None:
        self.t1v = min(self.config.t1_max_s, max(self.config.t1_min_s, 2.0 * self.srt))

    def _nr_error(self) -> None:
        """nr_error_recovery: DL-ERROR J and re-establish."""
        self.layer3_initiated = False
        self._establish(State.AWAITING_V22 if self.modulo == 128 else State.AWAITING_CONNECTION)
        self.events("reset", "reset-nr")

    def _rx_i(self, frame: Frame, command: bool, pf: bool) -> None:
        if not self.connected:
            if self.state is State.DISCONNECTED or self.state is State.AWAITING_RELEASE:
                if pf:
                    self._u(FrameType.DM, True, command=False)
            return
        if len(frame.info) > self.config.n1_rx:
            self.layer3_initiated = False
            self._establish(State.AWAITING_V22 if self.modulo == 128 else State.AWAITING_CONNECTION)
            self.events("reset", "reset-n1")
            return
        nr, ns = frame.control.nr, frame.control.ns
        if not self._nr_valid(nr):
            self._nr_error()
            return
        if self.state is State.CONNECTED:
            self._check_i_frame_acked(nr)
        else:
            self._ack_to(nr)
        if self.own_busy:
            if pf:
                self._s(FrameType.RNR, True, command=False)
                self.ack_pending = False
            return
        if self.adapter is not None and ns == self.vr:
            self.adapter.heard(self._rx_mode, None, self._now(), data=True)
            self._burst_rx += 1
        elif self.adapter is not None and not self.srej:
            self._burst_lost += 1
        if ns == self.vr:
            self.vr = (self.vr + 1) % self.modulo
            self.reject_exception = False
            self.stats["received_bytes"] += len(frame.info)
            self.deliver(frame.pid if frame.pid is not None else PID_NONE, frame.info)
            # Anything held behind it (SREJ) follows, in order.
            while self.vr in self._held:
                pid, info = self._held.pop(self.vr)
                self.vr = (self.vr + 1) % self.modulo
                self.stats["received_bytes"] += len(info)
                self.deliver(pid, info)
            if pf:
                self._enquiry_response(True)
            else:
                self.ack_pending = True
        elif self.srej:
            # Held if it is ahead of V(R) within the window; a copy of one
            # already delivered (a go-back-N retransmission) is dropped.
            # Either way the next transmission acknowledges and names what
            # is missing (frames_for_tx).
            if (ns - self.vr) % self.modulo < self.k and ns not in self._held:
                self._held[ns] = (frame.pid if frame.pid is not None else PID_NONE, frame.info)
                self.stats["held"] += 1
                if self.adapter is not None:
                    self.adapter.heard(self._rx_mode, None, self._now(), data=True)
                    self._burst_rx += 1
            if pf:
                self._enquiry_response(True)
            else:
                self.ack_pending = True
        elif self.reject_exception:
            if pf:
                self._enquiry_response(True)
        else:
            self.reject_exception = True
            self._s(FrameType.REJ, pf, command=False)
            self.ack_pending = False

    def _rx_s(self, frame: Frame, command: bool, pf: bool) -> None:
        if not self.connected:
            if pf and command and self.state in (State.DISCONNECTED, State.AWAITING_RELEASE):
                self._u(FrameType.DM, True, command=False)
            return
        kind, nr = frame.type, frame.control.nr
        was_busy = self.peer_busy
        self.peer_busy = kind is FrameType.RNR
        if self.peer_busy != was_busy:
            self.events("busy" if self.peer_busy else "ready", "peer-busy" if self.peer_busy else "peer-ready")
        now = self._now()
        if self.adapter is not None and command and pf and kind in (FrameType.RR, FrameType.RNR):
            self.adapter.poll(now)
        if self.state is State.CONNECTED:
            if command and pf:
                self._enquiry_response(True)
            if not self._nr_valid(nr):
                self._nr_error()
                return
            if kind is FrameType.REJ:
                self._ack_to(nr)
                self.t1.stop()
                self.t3.stop()
                self.t3.start(now, self.config.t3_s)
                self._retransmit_from(nr)
            else:
                self._check_i_frame_acked(nr)
            return
        # TIMER_RECOVERY
        if not command and pf:
            # The answer to our poll ends timer recovery. The round trip is
            # only measured when nothing was retried (Karn's rule).
            self._stop_t1_measured(now)
            if not self._nr_valid(nr):
                self._nr_error()
                return
            self._ack_to(nr)
            self.rc = 0
            if self.vs == self.va:
                self.t3.start(now, self.config.t3_s)
                self.state = State.CONNECTED
            else:
                self._retransmit_from(nr)
                self.state = State.CONNECTED
            return
        if command and pf:
            self._enquiry_response(True)
        if not self._nr_valid(nr):
            self._nr_error()
            return
        self._ack_to(nr)
        if kind is FrameType.REJ and self.vs != self.va:
            self._retransmit_from(nr)

    def _rx_srej(self, frame: Frame, command: bool, pf: bool) -> None:
        if not self.connected:
            return
        nr = frame.control.nr
        self.peer_busy = False
        entry = self._sent.get(nr)
        if entry is not None:
            pid, payload = entry
            control = Control(FrameType.I, False, nr=self.vr, ns=nr, modulo=self.modulo)
            self.stats["retransmitted"] += 1
            frame = self._frame(control, payload, pid=pid, command=True)
            self.stats["sent_I"] += 1
            # An I frame, so at the data mode on an adaptive link.
            self._send(Outgoing(frame.encode(), mode=self.tx_mode if self.adapter else None))
        if pf and not command and self.state is State.TIMER_RECOVERY:
            self.t1.stop()
            self.t1.start(self._now(), self.t1v)

    def _retransmit_from(self, nr: int) -> None:
        """invoke_retransmission: send again from N(R); _sent still holds
        each frame, so V(S) moving back is the whole of it."""
        self.vs = nr

    # -- state changes ----------------------------------------------------------------

    def _to(self, state: State, reason: str) -> None:
        self.state = state
        if state is State.DISCONNECTED:
            self.t1.stop()
            self.t3.stop()
            self._sent.clear()
            self._held.clear()
            self.srej = False
            self._closing = False
            self.adapter = None
            self.tx_mode = None
            self.peer_adapts = False
            self._announce_due = None
            self.events("disconnected", reason)
        elif state is State.CONNECTED:
            self._start_adapting()
            self.events("connected", reason)
            self._maybe_close()

    def _maybe_close(self) -> None:
        if self._closing and self.connected and not self._queue and not self._sent and self.vs == self.va:
            self._closing = False
            self.disconnect()
