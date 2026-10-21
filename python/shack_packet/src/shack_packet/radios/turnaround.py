"""Measuring a radio's key-up and unkey times, and the TXDELAY that follows.

TXDELAY is the preamble of flags sent from the moment of keying: long
enough for the radio to come up to power and for the far receiver's
squelch and bit clock to settle on what is left. Too short and every frame
is lost on that radio; too long and every transmission wastes air. The
channel never sends less than the link's `turnaround_s` plus a minimum
preamble (channel.MIN_PREAMBLE_S), so what matters is that `turnaround_s`
is the radio's real key-up time - which no manual here gives (the FTX-1's
CAT manual is silent; an IC-9700 reached half power 65 ms after a CI-V key
on Ray's bench, which is one radio on one band). So it is measured.

**How.** `measure(link, probe, confirm=CONFIRM)` keys the link a few times
for well under a second each (`overs`, `hold_s`), with a steady tone so
the radio's audio path is live, and watches a `Probe` that says whether the
radio is putting out RF:

- `RigPowerProbe(link)`: the radio's own output meter, asked as fast as it
  answers - an Icom's Po (CI-V 15 11), a Yaesu's PO (RM5;). Into a dummy
  load the reading rises when the transmitter does. GUIDE §5: on a 9700 the
  Po meter is the honest one (its ALC reads with any audio at all), and it
  drops out now and then, so a single zero after rising is not an unkey -
  two consecutive readings decide. Its threshold is relative, not fixed:
  `start()` reads the radio unkeyed first, because at RF POWER 0% the
  meter barely leaves the floor (2-4 of 255, 18/10 real-radio finding) and
  a threshold picked for full power never sees it.
- `LevelProbe`: a receiver that hears the station's own signal - an SDR,
  a second radio, a sound card on a monitor receiver - fed its audio; RF
  is "on" when the level is over a threshold above the quiet it heard
  first.

Key-up is the time from the key command leaving this computer
(`link.last_key_at`) to the probe first saying on; unkey, from the unkey
command to the probe saying off. The answer is the worst of the overs,
rounded up to 10 ms, as `turnaround_s`, and `txdelay_s` = that plus the
channel's minimum preamble. The unkey time is reported too: it is how long
after this station stops that it can hear an answer, which is the far
stations' TXDELAY problem, not this one's.

**Resolution** is the probe's: a Po read over the LAN is a round trip
(tens of ms), an audio level a block. `Measurement.resolution_s` says what
it was; each time is taken when the reading came back, an upper bound by
one reading, so a coarse probe errs towards a longer preamble.

**Safety.** This keys a transmitter with nobody sending frames, so it is
refused unless the caller passes `confirm=CONFIRM` - the app shows the
operator the band, the power and the words "into a dummy load" and passes
it only on their say-so (Brian's rule: never key a radio without his
presence; NB's diagnostic has the same shape: `--keyed` refused without
`--i-have-a-dummy-load-and-operator-present`). The link's own checks still
apply (the station must be armed, the watchdog runs); every over is
bounded (`hold_s` at most 2 s, `overs` at most 10), and the link is
unkeyed in a `finally` whatever happens.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass, field

from ..channel import MIN_PREAMBLE_S
from .base import AudioRadioLink, LinkRefused

#: What the caller passes to say the operator is present and the radio is
#: into a dummy load (module docstring).
CONFIRM = "dummy-load-and-operator-present"

MAX_OVERS = 10
MAX_HOLD_S = 2.0
#: How long to wait for RF to appear or go before calling the over failed.
DETECT_TIMEOUT_S = 2.0


class Probe:
    """Whether the radio is putting out RF now: True, False, or None when the
    probe could not tell this time (a reading that did not come back)."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def on_air(self) -> bool | None:
        raise NotImplementedError


class RigPowerProbe(Probe):
    """The radio's own output meter, asked each time: an Icom's Po (15 11)
    or a Yaesu's PO (RM5;). The meter is 0-255, and at RF POWER 0% a 9700's
    Po barely leaves the noise (GUIDE §5: 2-4 of 255 keyed, 0 unkeyed,
    18/10 real-radio finding) - a fixed threshold of 10 never saw it, so
    `measure()` returned no key-up time at all at minimum power.

    So the threshold is set relative to what the radio reads with nobody
    keyed: `start()` takes `baseline_readings` readings before the first
    key and sets it to the worst of them plus `margin` (so an unkeyed
    reading of 0 gives threshold 1, the 18/10 over's own number). Pass
    `threshold` to skip that and fix it outright - unchanged by `start()`,
    for a radio whose meter is known, or a test that wants one number."""

    def __init__(self, link, threshold: int | None = None, baseline_readings: int = 3, margin: int = 1):
        self.link = link
        #: None unless the caller fixed it; `start()` leaves it alone then.
        self._fixed = threshold
        #: What `on_air` compares against - the fixed value, or the default
        #: of 10 until `start()` has a baseline to replace it with.
        self.threshold = 10 if threshold is None else threshold
        self.baseline_readings = baseline_readings
        self.margin = margin

    def start(self) -> None:
        if self._fixed is not None:
            return
        readings = [r for r in (self._read() for _ in range(self.baseline_readings)) if r is not None]
        if readings:
            self.threshold = max(readings) + self.margin

    def _read(self) -> int | None:
        """The meter's raw 0-255 reading now, or None if it did not answer."""
        rig = self.link.rig
        if rig is None:
            return None
        if hasattr(rig, "radio_addr"):                 # an IcomRig
            from shack_rig.icom import civ
            got = self.link._call(rig.ask, civ.read_power(), b"\x15\x11", 0.5, timeout=1.0)
            if got is None or len(got) < 4:
                return None
            return civ.decode_meter(got[2:4])
        from shack_rig.yaesu import cat
        got = rig.ask(cat.read_meter("po"), "RM5", 0.5)
        if got is None:
            return None
        return cat.parse_meter(got)[1]

    def on_air(self) -> bool | None:
        v = self._read()
        return None if v is None else v >= self.threshold


class LevelProbe(Probe):
    """A receiver's audio level: `feed(pcm)` with 16-bit audio as it arrives
    (from an SDR, another radio, a monitor sound device). On when a block's
    RMS is `margin_db` over the median of the quiet blocks heard before the
    first over."""

    def __init__(self, margin_db: float = 12.0, quiet_blocks: int = 5):
        self.margin_db = margin_db
        self.quiet_blocks = quiet_blocks
        self._quiet: list[float] = []
        self._last: tuple[float, float] | None = None
        self._lock = threading.Lock()

    def feed(self, pcm: bytes) -> None:
        import array
        a = array.array("h")
        a.frombytes(bytes(pcm)[:len(pcm) // 2 * 2])
        if not a:
            return
        rms = math.sqrt(sum(x * x for x in a) / len(a)) + 1e-9
        db = 20 * math.log10(rms)
        with self._lock:
            if len(self._quiet) < self.quiet_blocks:
                self._quiet.append(db)
            self._last = (time.monotonic(), db)

    def ready(self) -> bool:
        return len(self._quiet) >= self.quiet_blocks

    def on_air(self) -> bool | None:
        with self._lock:
            if self._last is None or not self.ready():
                return None
            return self._last[1] > statistics.median(self._quiet) + self.margin_db


@dataclass
class Over:
    key_up_s: float | None
    unkey_s: float | None
    readings: int = 0


@dataclass
class Measurement:
    overs: list[Over] = field(default_factory=list)
    resolution_s: float = 0.0
    method: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.overs) and all(o.key_up_s is not None and o.unkey_s is not None for o in self.overs)

    @property
    def key_up_s(self) -> float | None:
        ups = [o.key_up_s for o in self.overs if o.key_up_s is not None]
        return max(ups) if ups else None

    @property
    def unkey_s(self) -> float | None:
        downs = [o.unkey_s for o in self.overs if o.unkey_s is not None]
        return max(downs) if downs else None

    @property
    def turnaround_s(self) -> float | None:
        """The worst key-up, rounded up to 10 ms. Each key-up is already an
        upper bound by one reading (`_watch`), so a coarse probe errs towards
        a longer preamble, never a shorter one."""
        up = self.key_up_s
        if up is None:
            return None
        return math.ceil(up * 100 - 1e-9) / 100

    @property
    def txdelay_s(self) -> float | None:
        t = self.turnaround_s
        return None if t is None else round(t + MIN_PREAMBLE_S, 3)

    def public(self) -> dict:
        return {"ok": self.ok, "method": self.method, "overs": [o.__dict__ for o in self.overs],
                "key_up_s": self.key_up_s, "unkey_s": self.unkey_s, "resolution_s": round(self.resolution_s, 4),
                "turnaround_s": self.turnaround_s, "txdelay_s": self.txdelay_s}


def _tone(rate: int, seconds: float, hz: float = 1700.0, level: float = 0.4) -> bytes:
    import array
    n = int(rate * seconds)
    return array.array("h", (int(level * 32767 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))).tobytes()


def _watch(probe: Probe, want: bool, since: float, clock, poll_s: float, steps: list) -> tuple[float | None, int]:
    """Poll until the probe says `want` twice running. The time is that of
    the first of the two readings coming back - an upper bound, by at most
    one reading, on when the radio changed - measured from `since`."""
    first = None
    n = 0
    end = clock() + DETECT_TIMEOUT_S
    while clock() < end:
        t = clock()
        got = probe.on_air()
        back = clock()
        steps.append(back - t)
        n += 1
        if got is want:
            if first is not None:
                return first - since, n
            first = back
        elif got is not None:
            first = None
        time.sleep(poll_s)
    return None, n


def measure(link: AudioRadioLink, probe: Probe, *, confirm: str = "", overs: int = 3, hold_s: float = 0.6,
            gap_s: float = 0.5, poll_s: float = 0.002, apply: bool = False) -> Measurement:
    """Key `link` `overs` times and time the radio with `probe` (module
    docstring). With `apply`, the link's turnaround_s becomes the result."""
    if confirm != CONFIRM:
        raise LinkRefused("setup", "a turnaround measurement keys the radio: the operator must be present "
                                   "and the radio into a dummy load (confirm=CONFIRM)")
    if not 1 <= overs <= MAX_OVERS or not 0.1 <= hold_s <= MAX_HOLD_S:
        raise LinkRefused("setup", f"1-{MAX_OVERS} overs of 0.1-{MAX_HOLD_S} s each")
    clock = link.clock
    result = Measurement(method=type(probe).__name__)
    steps: list[float] = []
    tone = _tone(link.rate, hold_s + 0.2)
    probe.start()
    try:
        for i in range(overs):
            link.key()
            link.send_pcm(tone)
            up, n1 = _watch(probe, True, link.last_key_at, clock, poll_s, steps)
            left = link.last_key_at + hold_s - clock()
            if left > 0:
                time.sleep(left)
            link.abort()
            down, n2 = _watch(probe, False, link.last_unkey_at, clock, poll_s, steps)
            result.overs.append(Over(up, down, n1 + n2))
            if up is None or down is None:
                break
            if i < overs - 1:
                time.sleep(gap_s)
    finally:
        link.abort()
        probe.stop()
    if steps:
        result.resolution_s = statistics.median(steps) + poll_s
    if apply and result.ok:
        link.turnaround_s = result.turnaround_s
        link.turnaround_measured = True
    return result
