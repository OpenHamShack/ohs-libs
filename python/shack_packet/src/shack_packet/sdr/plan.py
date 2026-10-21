"""Which channels the monitor listens to: data, never constants.

The default (channels.json beside this file) is 2 m packet as the UK and
most of IARU Region 1 work it: APRS on 144.800, the node channels 144.925,
144.9375 and 144.950, and the ISS on 145.825. It is a file rather than a
table in code because it is a band plan, and band plans belong to the
station's country and licence (GUIDE §1: band plans are shared data driven
by the station profile, never hard-coded): `from_spots()` takes the
band-plan dataset's own spot entries ({"hz", "label", "mode"}, as
data/bandplan/plans/*.json carry for APRS today), and the operator's own
list arrives through `from_dict()` in the same shape as the file.

A channel's `kind` is a KINDS key the app translates; its `label` is shown
as written (a frequency or a name like "APRS", which every language says
the same way).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources

from ..errors import PacketError

#: What a channel is for, as keys the app translates.
KINDS = {
    "aprs": "APRS",
    "node": "packet node and BBS channel",
    "satellite": "satellite packet",
    "other": "packet channel",
}

#: The usual spacing on 2 m; a plan may say otherwise.
SPACING_HZ = 12_500


@dataclass(frozen=True)
class Channel:
    """One channel: `id` is stable (the app keys its windows by it), `hz`
    the frequency on the air."""
    id: str
    hz: float
    label: str = ""
    kind: str = "other"

    def __post_init__(self):
        if self.kind not in KINDS:
            raise PacketError(f"channel kind {self.kind!r} is not one of {', '.join(KINDS)}")


@dataclass(frozen=True)
class ChannelPlan:
    channels: tuple[Channel, ...]
    spacing_hz: float = SPACING_HZ

    def __post_init__(self):
        ids = [c.id for c in self.channels]
        if len(set(ids)) != len(ids):
            raise PacketError("two channels in the plan share an id")
        if not self.channels:
            raise PacketError("a channel plan needs at least one channel")

    def __iter__(self):
        return iter(self.channels)

    def __len__(self):
        return len(self.channels)

    def span(self) -> tuple[float, float]:
        """The lowest and highest frequency any channel's signal reaches."""
        lo = min(c.hz for c in self.channels) - self.spacing_hz / 2
        hi = max(c.hz for c in self.channels) + self.spacing_hz / 2
        return lo, hi

    def centre_for(self, rate: float, avoid_dc_hz: float = 25_000.0) -> float | None:
        """A centre frequency that puts every channel inside `rate` with a
        margin at the edges, and none within `avoid_dc_hz` of the middle
        (where an RTL-SDR's DC spike and a Pluto's DC tracker sit); None if
        the plan cannot fit. The channels are usually lopsided (four near
        144.9, one at 145.825), so the middle of the span is tried first
        and then nudged until DC is clear."""
        lo, hi = self.span()
        usable = 0.8 * rate
        if hi - lo > usable:
            return None
        mid = (lo + hi) / 2
        step = self.spacing_hz / 2
        for k in range(0, int(usable // step) + 1):
            for c in (mid + k * step, mid - k * step):
                if hi - c > usable / 2 or c - lo > usable / 2:
                    continue
                if all(abs(ch.hz - c) >= avoid_dc_hz for ch in self.channels):
                    return c
        return None

    def to_dict(self) -> dict:
        return {"spacingHz": self.spacing_hz,
                "channels": [{"id": c.id, "hz": c.hz, "label": c.label, "kind": c.kind} for c in self.channels]}


def from_dict(data: dict) -> ChannelPlan:
    chans = tuple(Channel(str(c["id"]), float(c["hz"]), str(c.get("label", "")), str(c.get("kind", "other")))
                  for c in data["channels"])
    return ChannelPlan(chans, float(data.get("spacingHz", SPACING_HZ)))


def from_spots(spots, kind_of=None) -> ChannelPlan:
    """A plan from the band-plan dataset's spots ({"hz", "label", "mode"}).
    `kind_of(spot)` says each one's kind; by default "APRS" in the label
    makes it an APRS channel and anything else "other"."""
    def guess(spot):
        return "aprs" if "APRS" in str(spot.get("label", "")).upper() else "other"
    kind_of = kind_of or guess
    chans = tuple(Channel(f"ch-{int(s['hz'])}", float(s["hz"]), str(s.get("label", "")), kind_of(s)) for s in spots)
    return ChannelPlan(chans)


def default_plan() -> ChannelPlan:
    text = resources.files(__package__).joinpath("channels.json").read_text(encoding="utf-8")
    return from_dict(json.loads(text))
