"""What every real radio link does the same way: the safety core.

A link (IcomLink, YaesuLink, SoundcardLink) supplies a handful of radio
operations - key, unkey, a block of audio, the end of the audio, how long
the radio still has to play, busy - and this class does everything around
them, once, for all three:

**Keying is checked at the moment of keying.** `key()` asks the station
(callsign and the transmit switch, identity.Station.refusal) itself, as
the channel already has, and the claim on the radio, and refuses with a
reason key rather than keying. The check is repeated every supervisor tick
while keyed: an operator who disarms mid-transmission gets the radio
unkeyed within a tick (10 ms), not at the end of the over. On a rig link
the same test is also the lease the rig re-asks four times a second
(shack_rig.lease), so a stuck thread here cannot keep a radio keyed either.

**A hard limit on key-down.** `max_key_s` (15 s by default: the channel's
own limit on one transmission is 10 s, channel.ChannelConfig.max_tx_s) is
a watchdog, not a schedule: whatever the engine asked, a radio keyed for
longer is unkeyed and the app told (`watchdog`). The Icom app's
transmitter has the same shape (MAX_KEYED_S, digi/transmit.py).

**Unkey on any error, disconnect or close.** A radio operation that raises
unkeys at once; a radio that stops answering while keyed is unkeyed (or,
where the unkey itself cannot reach it, the unkey is owed and retried every
tick); `close()` always unkeys before anything else is let go.

**An unkey is confirmed, and there is a second watchdog.** A link that can
ask its radio whether it is transmitting (`_radio_ptt_readback`; the rig
links) asks after every unkey and sends the unkey again until the radio
says it is receiving, up to UNKEY_TRIES times, each retry a WARNING: an
unkey that was queued but never reached the radio is otherwise believed
by everyone (Ray's IC-9700 over USB, stuck on transmit twice on
18/10/2026, cause not yet known). And a thread of its own - not the
supervisor, which could be stuck holding the lock in a radio call - sends
the unkey straight to the radio (`_emergency_unkey`) if the link has been
keyed or keying for HARD_MARGIN_S past `max_key_s`, logging at ERROR.
`emergency_unkey()` is the same lock-free route for a process on its way
out: registered with atexit while the link is open, and called by an app's
crash handler.

**Key before the audio, unkey after it has played.** The engine hands over
a whole transmission at once (TXDELAY flags, frames, TXTAIL) and calls
`unkey()` when it would have ended on the air. Here the audio is paced
into the radio in 20 ms pieces against an absolute clock, no more than
LEAD_S ahead of real time - the Icom app's transmitter feeds the LAN
radio's jitter buffer the same way, for the reason it gives (CHUNK_MS) -
and the unkey waits for the last piece to be handed over and for the
radio's own buffer to play (`_radio_drain_s`, capped at MAX_DRAIN_S, as
digi/transmit.py caps its drain). A stopped transmission drops the key at
once. The TXDELAY the channel sends is never shorter than `turnaround_s`
plus a preamble (channel.MIN_PREAMBLE_S), so flags sent while the radio
comes up to power are flags nobody needed to hear.

**Received audio never runs the engine on the radio's thread.** A radio
hands its audio over from whatever thread it lives on - an Icom's asyncio
loop, which also carries every CI-V answer, the key and the unkey; a sound
card's capture thread - and the engine demodulates under its lock. Called
straight from there, as `_rx` once was, a demodulator that falls behind
delays every CI-V answer, and the radio's loop waiting for the engine's lock
while the engine waits on the loop to key is a deadlock that only `_call`'s
timeout broke: the IC-9700 on 18/10/2026 timed out set_ptt and the
minimum-power read, and went 15 s without answering its polls, only with
the app's engine running. So `_rx` only queues, and a thread of this link's
own (`_receive`) feeds the engine. The queue holds RX_QUEUE_S of audio; a
receiver further behind than that loses the oldest, counted in
`stats["rx_dropped"]` and logged when it starts, because late audio is
worth nothing to a channel-access decision and a radio's thread must never
wait for the engine.

**The claim seam.** On the OpenHamShack controller the broker owns the
hardware (GUIDE §1, PLATFORM-PLAN §1.3); the standalone desktop app talks
to the radio directly. `Claim` is where that difference will live:
`LocalClaim` (the desktop's: always ours) is the only one built, and a
broker-backed claim later implements the same three calls - acquire on
open, held() asked at every key and every tick, release on close.

Everything a link reports to the app goes through `sink.link_event(kind,
reason, detail)` with a key from REASONS, translated by the app as the
engine's other keys are.
"""

from __future__ import annotations

import atexit
import collections
import logging
import queue
import threading
import time
from typing import Callable

from ..channel import LinkSink, RadioLink
from ..errors import PacketError
from ..identity import REFUSALS, Station

log = logging.getLogger(__name__)

#: Why a link refused, unkeyed or complained, as keys the app translates
#: (as identity.REFUSALS, which it includes).
REASONS = {
    **REFUSALS,
    "not-open": "the radio link is not open",
    "claim-lost": "another program holds the radio",
    "receive-only": "this radio link is receive-only (no PTT is set up)",
    "radio-refused": "the radio would not key",
    "radio-error": "the radio link failed",
    "radio-auth": "the radio refused the username and password",
    "watchdog": "the transmitter was keyed longer than its limit and was unkeyed",
    "disarmed": "transmit was switched off during a transmission, which was cut short",
    "disconnected": "the radio went away",
    "unkey-failed": "the radio could not be told to stop transmitting; it is being told again",
    "unkey-sent": "the owed unkey reached the radio",
    "busy-stuck": "the radio has reported its channel busy for a long time; is its squelch open?",
    "vox": "PTT is by VOX: the radio keys on the audio after its own delay and holds on for its own "
           "hang time, which this station cannot measure or shorten",
    "setup": "the radio could not be set up for packet",
    "setup-report": "what the app set on the radio for packet, and what it found",
    "setup-restored": "the radio's settings were put back as they were found",
    "setup-recovered": "settings the last session left changed on the radio were put back",
    "setup-warning": "something about this radio to check before transmitting",
    "unkey-unconfirmed": "the radio still did not confirm it had stopped transmitting after the unkey was "
                         "sent several times: check it now",
    "not-keyed": "this link has not keyed the radio",
    "tx-not-quiet": "the radio's transmitter did not read back as switched off, so the radio is not used",
    "out-of-range": "the radio cannot tune that frequency",
    "tx-firmware": "this radio's firmware is too old to transmit safely: update it, and it will transmit",
    "tx-unguarded": "the transmitter guard on the radio is not running, so this radio is receive-only",
    "off-channel": "the radio is not on the channel the app confirmed (its VFO was turned), so it is not keyed",
    "off-mode": "the radio is not in FM, so it is not keyed",
}


class LinkRefused(PacketError):
    """A link would not key. `reason` is a REASONS key; `detail` says more,
    for a log or an app that wants it (the rig's own sentence, say)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class Claim:
    """Whether this app may use the radio now. See the module docstring."""

    def acquire(self) -> str | None:
        """Take the radio; None, or a REASONS key saying why not."""
        return None

    def held(self) -> bool:
        return True

    def release(self) -> None:
        pass


class LocalClaim(Claim):
    """The standalone desktop app's: the radio is on this computer and this
    app talks to it directly (GUIDE §1). Always held while open."""


class AudioRadioLink(RadioLink):
    """The safety core (module docstring). Subclasses implement the `_radio_*`
    operations and call `_rx(pcm)` with received audio and `_busy(flag)`
    when the radio's squelch changes; everything else is here."""

    #: How long a piece of transmit audio is, and how far ahead of real time
    #: the pieces may be handed to the radio.
    CHUNK_S = 0.02
    LEAD_S = 0.06
    #: The most the unkey waits for the radio to play what it has.
    MAX_DRAIN_S = 1.0
    #: The supervisor's tick: the watchdog's and the disarm check's resolution.
    TICK_S = 0.01
    #: A radio busy this long without a break is reported (once) as stuck.
    BUSY_STUCK_S = 60.0
    #: The most received audio waiting for the engine (module docstring):
    #: two seconds covers a garbage collection or a slow block on a busy
    #: laptop; a receiver further behind than that is not catching up.
    RX_QUEUE_S = 2.0

    #: What this kind of link assumes its key-up time is until a measurement
    #: says otherwise (radios.turnaround). Subclasses set their own.
    DEFAULT_TURNAROUND_S = 0.1

    #: The unkey's confirmation (module docstring): the first read-back this
    #: long after the unkey - after the radio's own poll answers that were
    #: already in flight, which could still say "transmitting" - then one
    #: per UNKEY_RETRY_S, the unkey sent again each time it is not
    #: confirmed, at most UNKEY_TRIES times before it is reported.
    UNKEY_CONFIRM_S = 0.3
    UNKEY_RETRY_S = 0.3
    UNKEY_TRIES = 5
    #: The hard watchdog: how far past max_key_s it acts, and how often it
    #: looks. A second, not a tick, past the supervisor's own watchdog, so
    #: the two never race on a healthy link.
    HARD_MARGIN_S = 1.0
    HARD_EVERY_S = 0.1

    def __init__(self, *, station: Station | None = None, claim: Claim | None = None,
                 max_key_s: float = 15.0, turnaround_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.station = station
        self.claim = claim or LocalClaim()
        self.max_key_s = max_key_s
        self.turnaround_s = self.DEFAULT_TURNAROUND_S if turnaround_s is None else float(turnaround_s)
        #: Whether turnaround_s was measured (radios.turnaround) or assumed.
        self.turnaround_measured = turnaround_s is not None
        self.clock = clock
        self.sink: LinkSink | None = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._open = False
        self._thread: threading.Thread | None = None
        self._keyed = False
        self._keyed_at = 0.0
        #: When key() began asking the radio to key, until it has its answer
        #: (None otherwise). The rig links' lease says "not keyed" unless the
        #: link is keyed or keying, so a key that reaches the rig late - after
        #: key() gave up on it and unkeyed - is refused by the rig itself.
        self._keying_since: float | None = None
        #: Since when the radio may be on the air because of this link: set
        #: as keying starts, cleared only once an unkey has reached the
        #: radio. What the hard watchdog times - not `_keyed`, which an
        #: abort clears even when its unkey could not be sent.
        self._air_since: float | None = None
        #: The unkey awaiting the radio's confirmation: [next read-back at,
        #: retries so far], or None.
        self._confirm: list | None = None
        self._hard: threading.Thread | None = None
        self._hard_stop = threading.Event()
        self._audio: collections.deque[bytes] = collections.deque()
        self._written_until = 0.0
        self._unkey_wanted = False
        self._ended_at: float | None = None
        self._drain_until = 0.0
        self._unkey_owed = False
        self._radio_busy: bool | None = None
        self._busy_since: float | None = None
        self._busy_reported = False
        #: When the radio was last told to key and to unkey, by this link's
        #: clock: what the turnaround tool measures from.
        self.last_key_at: float | None = None
        self.last_unkey_at: float | None = None
        #: Counters for a status page and the tests.
        self.stats = collections.Counter()
        self.events: collections.deque[tuple[str, str, dict]] = collections.deque(maxlen=200)
        #: Calls into the engine (busy_in, link_event), made in order by one
        #: thread of their own: never under this link's lock, because the
        #: engine takes its lock to record them and may be inside key()
        #: holding it; and never one thread each, which could reorder a busy
        #: that opened and closed.
        self._notices: queue.Queue = queue.Queue()
        self._notifier: threading.Thread | None = None
        #: Received audio on its way to the engine (module docstring):
        #: (pcm, samples, level) blocks (`_rx`), their total, and the thread that drains
        #: them. The condition's lock is held only to move a block in or
        #: out, never while the engine runs.
        self._rxq: collections.deque[tuple[object, int, tuple | None]] = collections.deque()
        self._rxq_samples = 0
        self._rxq_ready = threading.Condition(threading.Lock())
        self._rx_dropping = False
        self._receiver: threading.Thread | None = None

    # -- what the engine calls (channel.RadioLink) ---------------------------------

    def attach(self, sink: LinkSink) -> None:
        self.sink = sink
        if self.station is None:
            self.station = getattr(sink, "station", None)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def keyed(self) -> bool:
        return self._keyed

    def key(self) -> None:
        with self._lock:
            refusal = self._refusal()
            if refusal is not None:
                self.stats["refused"] += 1
                raise LinkRefused(refusal)
            if self._keyed:
                # The last transmission is still draining and another follows
                # at once (a digipeated frame, an acknowledgement): stay keyed
                # rather than drop and raise the carrier between them. The
                # watchdog still counts from the first key.
                self._unkey_wanted = False
                self._ended_at = None
                return
            self._confirm = None
            self._keying_since = self.clock()
            if self._air_since is None:
                self._air_since = self._keying_since
            try:
                self._radio_key()
            except LinkRefused:
                # Not keying any more before the unkey goes: a key still on
                # its way to the radio (a call that timed out) must meet a
                # lease that refuses it, not one that still permits.
                self._keying_since = None
                self._unkey_radio()
                raise
            except Exception as e:                  # noqa: BLE001 - any failure: unkey, refuse
                self._keying_since = None
                log.exception("keying failed")
                self._unkey_radio()
                raise LinkRefused("radio-refused", str(e)) from None
            now = self.clock()
            self._keyed = True
            self._keying_since = None
            self._keyed_at = now
            self.last_key_at = now
            self._written_until = now
            self._unkey_wanted = False
            self._ended_at = None
            self._audio.clear()
            self.stats["keyed"] += 1
        self._wake.set()

    def send_pcm(self, pcm: bytes) -> None:
        with self._lock:
            if not self._keyed:
                # Audio to an unkeyed radio is never sent: with VOX, or a
                # radio whose own PTT someone else holds, it would transmit.
                self.stats["audio_refused"] += 1
                return
            step = int(self.rate * self.CHUNK_S) * 2
            for at in range(0, len(pcm), step):
                self._audio.append(pcm[at:at + step])
        self._wake.set()

    def unkey(self) -> None:
        with self._lock:
            if not self._keyed:
                return
            self._unkey_wanted = True
        self._wake.set()

    def abort(self) -> None:
        """Stop transmitting now: drop what has not played and unkey without
        waiting for it (an operator's stop; the turnaround measurement)."""
        with self._lock:
            if self._keyed:
                self._abort_locked(None)

    # -- lifetime ---------------------------------------------------------------------

    def open(self) -> AudioRadioLink:
        """Claim the radio, bring it up, start the supervisor. Raises
        LinkRefused with the reason it could not."""
        with self._lock:
            if self._open:
                return self
            why = self.claim.acquire()
            if why is not None:
                raise LinkRefused(why)
            try:
                self._open_radio()
            except LinkRefused:
                self.claim.release()
                raise
            except Exception as e:                  # noqa: BLE001
                self.claim.release()
                log.exception("opening the radio")
                raise LinkRefused("radio-error", str(e)) from None
            self._open = True
            self._thread = threading.Thread(target=self._supervise, name=f"{type(self).__name__} supervisor",
                                            daemon=True)
            self._thread.start()
            self._hard_stop.clear()
            self._hard = threading.Thread(target=self._hard_watch, name=f"{type(self).__name__} hard watchdog",
                                          daemon=True)
            self._hard.start()
            atexit.register(self.emergency_unkey)
            if self._notifier is None:
                self._notifier = threading.Thread(target=self._notify, name=f"{type(self).__name__} notices",
                                                  daemon=True)
                self._notifier.start()
            if self._receiver is None:
                self._receiver = threading.Thread(target=self._receive, name=f"{type(self).__name__} receive",
                                                  daemon=True)
                self._receiver.start()
        return self

    def close(self) -> None:
        """Unkey first, then let go of everything. Safe twice."""
        if self._open:
            atexit.unregister(self.emergency_unkey)
        with self._lock:
            was_open = self._open
            self._open = False
            if self._keyed or self._keying_since is not None:
                self._abort_locked(None)
            self._confirm = None
        self._wake.set()
        self._hard_stop.set()
        hard, self._hard = self._hard, None
        if hard is not None and hard is not threading.current_thread():
            hard.join(timeout=2)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        if was_open:
            try:
                self._close_radio()
            except Exception:                       # noqa: BLE001 - closing regardless
                log.exception("closing the radio")
            finally:
                self.claim.release()
        notifier, self._notifier = self._notifier, None
        if notifier is not None:
            self._notices.put(None)
            if notifier is not threading.current_thread():
                notifier.join(timeout=2)
        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            with self._rxq_ready:
                self._rxq_ready.notify()
            if receiver is not threading.current_thread():
                receiver.join(timeout=2)

    def _notify(self) -> None:
        while True:
            item = self._notices.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:                       # noqa: BLE001 - the engine's problem, logged
                log.exception("telling the engine")
    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- the supervisor -------------------------------------------------------------------

    def _supervise(self) -> None:
        while self._open:
            self._wake.wait(self.TICK_S)
            self._wake.clear()
            events = []
            try:
                with self._lock:
                    if not self._open:
                        break
                    self._tick(events)
            except Exception:                       # noqa: BLE001 - the supervisor must not die
                log.exception("radio link supervisor")
                with self._lock:
                    if self._keyed:
                        self._abort_locked("radio-error", events)
            for kind, reason, detail in events:
                self._event(kind, reason, detail)
            if self._confirm is not None:
                self._confirm_unkey()

    def _confirm_unkey(self) -> None:
        """Ask the radio whether the last unkey took, and send it again if
        not (module docstring). The read-back is made outside the lock: it
        is a round trip to the radio, and key() must not wait behind it. A
        key in the meantime cancels the confirmation - transmitting is then
        what the radio should be doing."""
        pending = self._confirm
        if pending is None or self.clock() < pending[0]:
            return
        try:
            keyed = self._radio_ptt_readback()
        except Exception as e:                      # noqa: BLE001 - an unanswered read, as None
            log.debug("PTT read-back failed: %s", e)
            keyed = None
        events = []
        with self._lock:
            if self._confirm is not pending or self._keyed or self._keying_since is not None:
                return
            if keyed is False:
                self._confirm = None
                if pending[1]:
                    log.warning("the radio confirmed receive after %d repeated unkey(s)", pending[1])
                return
            tries = pending[1] + 1
            if tries > self.UNKEY_TRIES:
                self._confirm = None
                self.stats["unkey-unconfirmed"] += 1
                log.error("the radio has not confirmed receive after %d unkeys (%s); check it now",
                          tries, "it still reports transmit" if keyed else "it does not answer a PTT read")
                events.append(("radio", "unkey-unconfirmed", {"tries": tries, "keyed": keyed}))
            else:
                self.stats["unkey-retried"] += 1
                log.warning("the radio %s after the unkey; sending it again (%d of %d)",
                            "still reports transmit" if keyed else "did not answer a PTT read", tries,
                            self.UNKEY_TRIES)
                try:
                    self._radio_unkey()
                except Exception as e:              # noqa: BLE001 - counted as a try; the next one follows
                    log.warning("the repeated unkey failed: %s", e)
                self._confirm = [self.clock() + self.UNKEY_RETRY_S, tries]
        for e in events:
            self._event(*e)

    # -- the hard watchdog and the way out ----------------------------------------------

    def _hard_watch(self) -> None:
        """The second watchdog (module docstring). Reads the keyed state
        without the lock - the lock is what a stuck supervisor or a stuck
        key() would be holding - and never clears it: it only gets the
        radio off the air, and leaves the bookkeeping to the supervisor
        once that runs again."""
        next_fire = 0.0
        while not self._hard_stop.wait(self.HARD_EVERY_S):
            since = self._air_since
            if since is None:
                next_fire = 0.0
                continue
            now = self.clock()
            held = now - since
            if held <= self.max_key_s + self.HARD_MARGIN_S or now < next_fire:
                continue
            self.stats["hard-watchdog"] += 1
            log.error("hard watchdog: the radio has been keyed %.1f s, past its %.0f s limit, and the link has "
                      "not unkeyed it; sending the unkey directly", held, self.max_key_s)
            try:
                self._emergency_unkey()
            except Exception:                       # noqa: BLE001 - it tries again in a second
                log.exception("hard watchdog: the direct unkey failed")
            next_fire = now + 1.0

    def emergency_unkey(self) -> None:
        """Unkey without the lock, whatever this link believes: for a
        process on its way out (atexit, an app's crash handler) and the hard
        watchdog. An unkey sent to a radio already receiving does nothing,
        so this does not first ask whether one is needed."""
        if not self._open:
            return                                  # close() unkeyed it, under the lock, first
        try:
            self._emergency_unkey()
        except Exception:                           # noqa: BLE001 - on the way out regardless
            log.exception("emergency unkey")

    def _tick(self, events: list) -> None:
        now = self.clock()
        if self._unkey_owed:
            try:
                self._radio_unkey()
                self._unkey_owed = False
                self._air_since = None
                self.last_unkey_at = now
                self._await_confirmation()
                events.append(("radio", "unkey-sent", {}))
            except Exception as e:                  # noqa: BLE001
                log.debug("owed unkey still failing: %s", e)
        if self._keyed:
            why = self._refusal()
            if why is not None:
                self._abort_locked("disarmed" if why in REFUSALS else why, events)
                return
            if now - self._keyed_at > self.max_key_s:
                self._abort_locked("watchdog", events, {"seconds": round(now - self._keyed_at, 2)})
                return
            if not self._radio_alive():
                self._abort_locked("disconnected", events)
                return
            self._pace(now, events)
            if not self._keyed:
                return
            if self._unkey_wanted and not self._audio:
                if self._ended_at is None:
                    self._radio_audio_end()
                    self._ended_at = now
                    drain = max(0.0, self._written_until - now) + max(0.0, self._radio_drain_s())
                    self._drain_until = now + min(self.MAX_DRAIN_S, drain)
                if now >= self._drain_until:
                    self._unkey_radio()
                    self._keyed = False
        busy = self._radio_busy_now()
        if busy is not None:
            self._busy_changed(busy, now, events)

    def _pace(self, now: float, events: list) -> None:
        if self._written_until < now:
            self._written_until = now
        while self._audio and self._written_until < now + self.LEAD_S:
            chunk = self._audio.popleft()
            try:
                ok = self._radio_audio(chunk)
            except Exception as e:                  # noqa: BLE001
                log.warning("transmit audio failed: %s", e)
                ok = False
            if ok is False:
                self._abort_locked("radio-error", events, {"what": "audio"})
                return
            self._written_until += len(chunk) / 2 / self.rate
            self.stats["audio_s"] += len(chunk) / 2 / self.rate

    def _abort_locked(self, reason: str | None, events: list | None = None, detail: dict | None = None) -> None:
        """Drop the audio and unkey now. `reason` None is an ordinary close."""
        self._audio.clear()
        try:
            self._radio_audio_abort()
        except Exception:                           # noqa: BLE001
            log.exception("dropping transmit audio")
        self._unkey_radio()
        self._keyed = False
        self._unkey_wanted = False
        if reason is not None:
            self.stats[reason] += 1
            log.warning("unkeyed: %s", reason)
            if events is not None:
                events.append(("unkeyed", reason, detail or {}))

    def _unkey_radio(self) -> None:
        self._keying_since = None
        try:
            self._radio_unkey()
            self._unkey_owed = False
            self._air_since = None
            self.last_unkey_at = self.clock()
            self._await_confirmation()
        except Exception as e:                      # noqa: BLE001 - owed, and said
            log.error("unkey failed, will retry: %s", e)
            self._unkey_owed = True
            self._event("radio", "unkey-failed", {"error": str(e)})

    def _await_confirmation(self) -> None:
        if self.CONFIRMS_UNKEY and self._open:
            self._confirm = [self.clock() + self.UNKEY_CONFIRM_S, 0]

    def _refusal(self) -> str | None:
        if not self._open:
            return "not-open"
        if not self.claim.held():
            return "claim-lost"
        station = self.station
        if station is None:
            return "no-callsign"
        return station.refusal()

    # -- receive side helpers for subclasses ------------------------------------------

    def _rx(self, pcm, level: tuple[float, float] | None = None) -> None:
        """Received audio, from the radio's own thread: queued, never handed
        to the engine here (module docstring). Returns at once whatever the
        engine is doing. `level`, from a link that measures its channel (an
        SDR's receiver), is (dB over the floor, noise bandwidth in Hz) as
        this audio was received: it rides in the queue with the audio and
        reaches the engine (`level_in`) just before it, so a frame decoded
        from it carries its own level, however far behind the engine is."""
        if self._receiver is None:
            return                                  # not open: nobody to hear it
        n = len(pcm) // 2 if isinstance(pcm, (bytes, bytearray, memoryview)) else len(pcm)
        limit = self.RX_QUEUE_S * self.rate
        dropped = 0
        with self._rxq_ready:
            self._rxq.append((pcm, n, level))
            self._rxq_samples += n
            while self._rxq_samples > limit and len(self._rxq) > 1:
                _old, m, _level = self._rxq.popleft()
                self._rxq_samples -= m
                dropped += 1
            self._rxq_ready.notify()
        if dropped:
            self.stats["rx_dropped"] += dropped
            if not self._rx_dropping:
                self._rx_dropping = True
                log.warning("the receiver is more than %.1f s behind the radio; dropping the oldest audio",
                            self.RX_QUEUE_S)

    def _receive(self) -> None:
        """The receive thread: the engine's demodulator runs here."""
        me = threading.current_thread()
        while True:
            with self._rxq_ready:
                while not self._rxq and self._receiver is me:
                    self._rxq_ready.wait()
                if self._receiver is not me:
                    self._rxq.clear()
                    self._rxq_samples = 0
                    return
                pcm, n, level = self._rxq.popleft()
                self._rxq_samples -= n
                if not self._rxq and self._rx_dropping:
                    self._rx_dropping = False
                    log.info("the receiver has caught up (%d blocks dropped so far)", self.stats["rx_dropped"])
            sink = self.sink
            if sink is None:
                continue
            try:
                level_in = getattr(sink, "level_in", None) if level is not None else None
                if level_in is not None:
                    level_in(*level)
                if hasattr(pcm, "parts"):
                    sink.mode_in(pcm)               # an adaptive link's modes (radios.sdrrx.ModeBundle)
                else:
                    sink.pcm_in(pcm)
            except Exception:                       # noqa: BLE001 - the engine's problem, logged
                log.exception("receive audio into the engine")

    def _busy(self, busy: bool | None) -> None:
        """Push form of busy, for a link whose radio reports changes."""
        if busy is None:
            return
        events = []
        with self._lock:
            self._busy_changed(bool(busy), self.clock(), events)
        for e in events:
            self._event(*e)

    def _busy_changed(self, busy: bool, now: float, events: list) -> None:
        if busy and self._busy_since is None:
            self._busy_since = now
        if not busy:
            self._busy_since = None
            self._busy_reported = False
        if (busy and not self._busy_reported and self._busy_since is not None
                and now - self._busy_since > self.BUSY_STUCK_S):
            self._busy_reported = True
            events.append(("radio", "busy-stuck", {"seconds": round(now - self._busy_since)}))
        if busy != self._radio_busy:
            self._radio_busy = busy
            sink = self.sink
            if sink is not None:
                self._notices.put((sink.busy_in, (busy,)))

    def _event(self, kind: str, reason: str, detail: dict | None = None) -> None:
        self.events.append((kind, reason, dict(detail or {})))
        sink = self.sink
        if sink is not None:
            self._notices.put((sink.link_event, (kind, reason, dict(detail or {}))))

    # -- what a link implements --------------------------------------------------------

    def _open_radio(self) -> None:
        """Bring the radio up (and set it up for packet). Raise LinkRefused
        with a reason, or anything else for radio-error."""

    def _close_radio(self) -> None:
        """Put the radio back as found and let go of it. Already unkeyed."""

    def _radio_key(self) -> None:
        raise NotImplementedError

    def _radio_unkey(self) -> None:
        raise NotImplementedError

    def _radio_audio(self, pcm: bytes) -> bool:
        """One piece of transmit audio (s16le at `rate`). False or raise to
        abort the transmission."""
        raise NotImplementedError

    def _radio_audio_end(self) -> None:
        """The last piece has been handed over."""

    def _radio_audio_abort(self) -> None:
        """Drop whatever has not been played."""

    def _radio_drain_s(self) -> float:
        """How long the radio still has to play after the last piece."""
        return 0.0

    def _radio_alive(self) -> bool:
        return True

    #: Whether `_radio_ptt_readback` can answer, and so every unkey is confirmed.
    CONFIRMS_UNKEY = False

    def _radio_ptt_readback(self) -> bool | None:
        """Ask the radio itself: True transmitting, False receiving, None
        no answer. Only called where CONFIRMS_UNKEY."""
        return None

    def _emergency_unkey(self) -> None:
        """Get the radio off the air from any thread, without the lock and
        without anything that a stuck supervisor might be blocking. The
        default is the ordinary unkey; a link with a loop of its own (Icom)
        goes round it."""
        self._radio_unkey()

    def _radio_busy_now(self) -> bool | None:
        """Polled each tick: the radio's busy, or None (pushed instead, or
        the radio has none)."""
        return None
