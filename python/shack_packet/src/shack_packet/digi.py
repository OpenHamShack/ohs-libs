"""A digipeater: repeat frames whose path asks for this station.

Off unless the operator turns it on (`DigiConfig.enabled`), because a
digipeater transmits other people's traffic on its own licence and timing,
and nobody should find their station doing that by surprise. Everything it
does is configurable; the defaults are what direwolf's documented
`DIGIPEAT 0 0 ^WIDE[3-7]-[1-7]$|^TEST$ ^WIDE[12]-[12]$ TRACE` does for a
station in the UK APRS network, minus the aliases (none until set).

**What it repeats** - only the first hop in the path not yet repeated
(the H bit), so that a frame goes through the digipeaters in the order its
sender asked:

- *its own callsign* (exactly, SSID included): marked repeated. This is how
  connected-mode stations go through a digipeater (G0ABC>GB7BBS,GM4BRI-1).
- *an alias* (`aliases`, e.g. a town name, or WIDE1-1 for a fill-in
  digipeater): replaced by our callsign, marked repeated, so the path
  records who repeated it.
- *WIDEn-N* (`wide`, n up to `wide_max`): the New-N paradigm as direwolf
  does it. N=1: replaced by our callsign, repeated. N above 1: our callsign
  is inserted before it, repeated, and N goes down by one, so the next
  digipeater sees WIDEn-(N-1). WIDEn-N with n above `wide_max` is dropped
  (`wide_excess="drop"`) or, as the UK network's "path trap", repeated once
  as our callsign and no further (`"trap"`). `fill_in` limits it to WIDE1-1.

**Duplicate suppression**: a UI frame with the same source, destination,
PID and information as one this station repeated or sent within `dedupe_s`
(30 s, direwolf's) is not repeated again - otherwise two digipeaters that
hear each other repeat one WIDE3-3 packet back and forth until its hops run
out. Only UI frames: a connected-mode frame sent again is a retransmission
its sender needs repeated, and the first version, which deduplicated every
frame, dropped the second of two I frames carrying the same bytes and
stalled a transfer through it.

**Preemptive digipeating** (`preempt`, direwolf's option of that name, off
by default): when our callsign or an alias appears later in the path than
the first unrepeated hop, "drop" removes the unrepeated hops before it and
"mark" marks them repeated, then it is handled as above. direwolf's TRACE
variant (keep the skipped hops' calls) is not offered. Never for WIDEn-N.

Never repeated: frames from this station, and frames whose next hop is
nobody we answer to. Repeated frames go out without the persistence draw
(channel.py), as direwolf's do.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

from .ax25 import MAX_DIGIS, Address, Frame
from .errors import PacketError
from .identity import Station

_WIDE = re.compile(r"^WIDE([1-7])$")


@dataclass
class DigiConfig:
    enabled: bool = False
    aliases: list[str] = field(default_factory=list)
    wide: bool = True
    wide_max: int = 2
    wide_excess: str = "drop"
    fill_in: bool = False
    preempt: str = "off"
    dedupe_s: float = 30.0
    #: The SSID the digipeater answers to and signs with: None for the
    #: station's own (often -1 or -10 is kept for the digipeater).
    ssid: int | None = None

    def __post_init__(self):
        if self.wide_excess not in ("drop", "trap"):
            raise PacketError(f"wide_excess {self.wide_excess!r}: drop or trap")
        if self.preempt not in ("off", "drop", "mark"):
            raise PacketError(f"preempt {self.preempt!r}: off, drop or mark")


def _same(a: Address, b: Address) -> bool:
    return a.call == b.call and a.ssid == b.ssid


class Digipeater:
    def __init__(self, engine, config: DigiConfig | None = None):
        self.engine = engine
        self.config = config or DigiConfig()
        self._recent: collections.OrderedDict[tuple, float] = collections.OrderedDict()
        self.stats = collections.Counter()
        engine.digipeater = self

    @property
    def station(self) -> Station:
        return self.engine.station

    def _mycall(self) -> Address:
        return self.station.address(self.config.ssid)

    def _aliases(self) -> list[Address]:
        out = []
        for text in self.config.aliases:
            try:
                out.append(Address.from_text(text, flag=False))
            except ValueError:
                continue
        return out

    @staticmethod
    def _key(frame: Frame) -> tuple:
        return (str(frame.source), str(frame.destination), frame.pid, frame.control.type, frame.info)

    def _seen(self, frame: Frame, now: float) -> bool:
        horizon = now - self.config.dedupe_s
        while self._recent and next(iter(self._recent.values())) < horizon:
            self._recent.popitem(last=False)
        return self._key(frame) in self._recent

    def _remember(self, frame: Frame, now: float) -> None:
        key = self._key(frame)
        self._recent.pop(key, None)
        self._recent[key] = now

    # -- the engine's hooks ------------------------------------------------------

    def sent(self, frames: list[bytes], now: float) -> None:
        """Everything this station transmits, so it does not repeat its own
        frames when another digipeater sends them back."""
        from .ax25 import parse
        for raw in frames:
            frame = parse(raw)
            if frame is not None:
                self._remember(frame, now)

    def heard(self, frame: Frame, now: float) -> None:
        cfg = self.config
        if not cfg.enabled or self.station.callsign is None:
            return
        if self.station.owns(frame.source):
            return
        out = self.decide(frame)
        if out is None:
            return
        if frame.is_ui and self._seen(frame, now):
            self.stats["duplicate"] += 1
            return
        self._remember(frame, now)
        self.stats["repeated"] += 1
        self.engine.repeat(out)

    # -- the rules --------------------------------------------------------------------

    def decide(self, frame: Frame) -> Frame | None:
        """The frame as this digipeater would send it, or None."""
        cfg = self.config
        via = list(frame.via)
        first = next((i for i, hop in enumerate(via) if not hop.repeated), None)
        if first is None:
            return None
        mycall = self._mycall()
        aliases = self._aliases()

        def ours(hop: Address) -> str | None:
            if _same(hop, mycall):
                return "own"
            if any(_same(hop, a) for a in aliases):
                return "alias"
            return None

        used = lambda a: Address(a.call, a.ssid, True, reserved=a.reserved)  # noqa: E731
        me = Address(mycall.call, mycall.ssid, True)
        hop = via[first]
        match = ours(hop)
        if match is None and cfg.preempt != "off":
            later = next((j for j in range(first + 1, len(via)) if not via[j].repeated and ours(via[j])), None)
            if later is not None:
                if cfg.preempt == "drop":
                    del via[first:later]
                else:
                    for j in range(first, later):
                        via[j] = used(via[j])
                    first = later
                hop = via[first]
                match = ours(hop)
                self.stats["preempted"] += 1
        if match == "own":
            via[first] = used(hop)
            return self._with(frame, via)
        if match == "alias":
            via[first] = me
            return self._with(frame, via)
        wide = _WIDE.match(hop.call)
        if not cfg.wide or wide is None:
            return None
        n, remaining = int(wide.group(1)), hop.ssid
        if remaining < 1 or remaining > n:
            return None
        if cfg.fill_in and n != 1:
            return None
        if n > cfg.wide_max:
            if cfg.wide_excess == "drop":
                self.stats["excess"] += 1
                return None
            via[first] = me
            return self._with(frame, via)
        if remaining == 1:
            via[first] = me
        else:
            via[first] = Address(hop.call, remaining - 1, False, reserved=hop.reserved)
            if len(via) < MAX_DIGIS:
                via.insert(first, me)
        return self._with(frame, via)

    @staticmethod
    def _with(frame: Frame, via: list[Address]) -> Frame:
        return Frame(source=frame.source, destination=frame.destination, via=via, control=frame.control,
                     pid=frame.pid, info=frame.info)

    def poll(self, now: float) -> None:
        pass

    def next_deadline(self):
        return None
