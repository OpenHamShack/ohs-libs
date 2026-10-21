"""What the SDR monitor knows, as plain data the app draws (no Qt here).

The pipeline (sdr/pipeline.py) hands every decoded frame to `Monitor.add`
and every block's channel levels to `Monitor.levels`; the app reads the
tables, or `snapshot()` for all of it at once, from any thread (one lock;
the pipeline holds it for microseconds a frame). `rev` goes up with every
change, so a window redraws only when there is something new, and
`listeners` are called with each Decoded as it arrives - on the
pipeline's thread, so a Qt app marshals them to its own.

The tables, and what each is for:

- **frames**: the last `history` frames, every channel, oldest first: time,
  channel, level, SNR, frequency error, FX.25 tag and corrections, frame
  type, path, the monitor line and the APRS reading. The per-channel
  windows filter it.
- **heard**: one row a station - first and last heard, frames, which
  channels, heard direct or through which digipeaters (ax25.Frame.
  heard_from: the transmitter this receiver actually heard), its last level.
- **nodes**: stations that behave like nodes or BBSs, and why (ROLES keys):
  a NET/ROM NODES broadcast (with its alias and the routes it advertises),
  answering connections (UA), connected-mode traffic at all, NET/ROM
  traffic, or calling itself a BBS in a beacon.
- **links**: who talks to whom - connected-mode frames between two
  stations, and APRS messages from one to another - with counts each way.
- **activity**: per channel, frames, bytes, airtime and busy time (the
  receiver's carrier detect) in buckets of `bucket_s`, the last `buckets`.
- **positions**: APRS positions for the map, the latest per station or
  object plus a short track.
- **spectrum**: the last `spectrum_rows` waterfall rows (sdr/spectrum.py)
  and their frequencies.
- **channels**: each channel's current level, noise floor and totals.

`ppm_estimate()` reads the receiver's crystal error from the frames heard:
every station's measured frequency error, the median of them in ppm. One
station off frequency does not move a median; a receiver 30 ppm out
moves every station 4.3 kHz and the median with them.

The monitor only ever reads: it has no radio and imports nothing that can
key one. Frames go to the engine's heard table only if the app asks
(`feed_heard`), and then through AprsStation.hear, which never answers.
"""

from __future__ import annotations

import collections
import math
import re
import threading
from dataclasses import dataclass, field

from ..aprs import Packet
from ..ax25 import PID_NETROM, PID_NONE, Frame, FrameType
from ..netrom import NodesBroadcast, Route
from .plan import ChannelPlan

#: Why a station is listed as a node, as keys the app translates.
ROLES = {
    "netrom-node": "sends NET/ROM routing broadcasts",
    "netrom": "carries NET/ROM traffic",
    "answers": "answers connections",
    "connected": "works connected-mode packet",
    "bbs": "announces itself as a BBS",
}

CONNECTED = frozenset({FrameType.I, FrameType.RR, FrameType.RNR, FrameType.REJ, FrameType.SREJ, FrameType.SABM,
                       FrameType.SABME, FrameType.UA, FrameType.DM, FrameType.DISC, FrameType.FRMR})
_BBS_WORD = re.compile(r"\b(BBS|PBBS|MAILBOX)\b", re.I)
TRACK_POINTS = 50


@dataclass
class Decoded:
    seq: int
    time: float                 # UTC of the frame's start
    channel: str                # Channel.id
    hz: float
    raw: bytes                  # without the FCS
    frame: Frame | None         # None: an FCS-good frame that is not AX.25
    level_db: float | None = None
    snr_db: float | None = None
    offset_hz: float | None = None
    fx25: int | None = None     # the FX.25 tag, None for plain AX.25
    fx25_check: int | None = None
    corrected: int = 0
    aprs: Packet | None = None
    nodes: NodesBroadcast | None = None

    @property
    def type(self) -> str:
        return self.frame.type.value if self.frame is not None else ""

    @property
    def text(self) -> str:
        return self.frame.monitor() if self.frame is not None else self.raw.hex()

    def as_dict(self) -> dict:
        f = self.frame
        out = {"seq": self.seq, "time": self.time, "channel": self.channel, "hz": self.hz,
               "levelDb": self.level_db, "snrDb": self.snr_db, "offsetHz": self.offset_hz,
               "fx25": self.fx25, "fx25Check": self.fx25_check, "corrected": self.corrected,
               "type": self.type, "text": self.text}
        if f is not None:
            out.update({"source": str(f.source), "destination": str(f.destination), "path": f.path,
                        "heardFrom": f.heard_from, "digipeated": f.digipeated})
        if self.aprs is not None:
            out["aprsKind"] = self.aprs.kind
        return out


@dataclass
class HeardStation:
    call: str
    first: float
    last: float
    frames: int = 0
    channels: collections.Counter = field(default_factory=collections.Counter)
    direct: int = 0
    via: collections.Counter = field(default_factory=collections.Counter)
    level_db: float | None = None
    last_text: str = ""


@dataclass
class Node:
    call: str
    first: float
    last: float
    roles: set = field(default_factory=set)
    alias: str = ""
    routes: list[Route] = field(default_factory=list)
    channels: collections.Counter = field(default_factory=collections.Counter)


@dataclass
class Link:
    a: str                      # the two stations, in alphabetical order
    b: str
    kind: str                   # "ax25" (connected mode) or "aprs-message"
    first: float
    last: float
    a_to_b: int = 0
    b_to_a: int = 0
    channels: collections.Counter = field(default_factory=collections.Counter)
    types: collections.Counter = field(default_factory=collections.Counter)


@dataclass
class PositionReport:
    name: str                   # the station, or the object's or item's name
    lat: float
    lon: float
    time: float
    channel: str
    symbol: str = ""
    comment: str = ""
    source: str = ""            # who sent it (differs from name for an object)
    track: list = field(default_factory=list)   # [(time, lat, lon)], oldest first


@dataclass
class Bucket:
    start: float
    frames: int = 0
    bytes: int = 0
    airtime_s: float = 0.0
    busy_s: float = 0.0


@dataclass
class ChannelState:
    level_db: float | None = None
    floor_db: float | None = None
    frames: int = 0
    fx25: int = 0
    activity: collections.deque = field(default_factory=collections.deque)


def airtime_s(raw: bytes, fx25_tag: int | None, baud: float = 1200.0) -> float:
    """How long a frame was on the air (without TXDELAY): its stuffed bits,
    or an FX.25 block's 64-bit tag and whole code block."""
    from ..demod import _line_bits
    from ..fx25 import TAGS
    if fx25_tag in TAGS:
        return (64 + 8 * TAGS[fx25_tag].block) / baud
    return (_line_bits(raw) + 16) / baud


class Monitor:
    def __init__(self, plan: ChannelPlan, *, history: int = 5000, bucket_s: float = 60.0, buckets: int = 60,
                 spectrum_rows: int = 300, heard_max: int = 5000):
        self.plan = plan
        self.lock = threading.RLock()
        self.rev = 0
        self.seq = 0
        self.bucket_s = float(bucket_s)
        self.buckets = int(buckets)
        self.heard_max = heard_max
        self.frames: collections.deque[Decoded] = collections.deque(maxlen=history)
        self.heard: collections.OrderedDict[str, HeardStation] = collections.OrderedDict()
        self.nodes: dict[str, Node] = {}
        self.links: dict[tuple, Link] = {}
        self.positions: dict[str, PositionReport] = {}
        self.channels: dict[str, ChannelState] = {c.id: ChannelState() for c in plan}
        self.spectrum: collections.deque = collections.deque(maxlen=spectrum_rows)
        self.spectrum_freqs = None
        self.listeners: list = []
        self.source_status: dict = {}
        self._offsets: collections.deque = collections.deque(maxlen=200)    # (hz on air, offset)

    # -- in from the pipeline -------------------------------------------------

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def add(self, d: Decoded) -> None:
        with self.lock:
            self.frames.append(d)
            st = self.channels[d.channel]
            st.frames += 1
            st.fx25 += d.fx25 is not None
            b = self._bucket(d.channel, d.time)
            b.frames += 1
            b.bytes += len(d.raw)
            b.airtime_s += airtime_s(d.raw, d.fx25)
            if d.offset_hz is not None and d.snr_db is not None and d.snr_db >= 10:
                self._offsets.append((d.hz, d.offset_hz))
            if d.frame is not None:
                self._heard(d)
                self._roles(d)
                self._links(d)
                self._position(d)
            self.rev += 1
        for fn in list(self.listeners):
            fn(d)

    def levels(self, channel: str, level_db: float | None, floor_db: float | None) -> None:
        with self.lock:
            st = self.channels[channel]
            st.level_db, st.floor_db = level_db, floor_db

    def busy(self, channel: str, at: float, seconds: float) -> None:
        with self.lock:
            self._bucket(channel, at).busy_s += seconds

    def add_rows(self, rows, freqs) -> None:
        if not rows:
            return
        with self.lock:
            self.spectrum.extend(rows)
            self.spectrum_freqs = freqs
            self.rev += 1

    # -- the tables -----------------------------------------------------------

    def _bucket(self, channel: str, t: float) -> Bucket:
        q = self.channels[channel].activity
        start = math.floor(t / self.bucket_s) * self.bucket_s
        if q and q[-1].start == start:
            return q[-1]
        if not q or start > q[-1].start:
            b = Bucket(start)
            q.append(b)
            while len(q) > self.buckets:
                q.popleft()
            return b
        # Late (a frame is reported after the block it ended in, and a
        # FX.25 one after its whole code block): into its own bucket if that
        # is still kept, in order; older than everything kept, nowhere.
        for i, b in enumerate(q):
            if b.start == start:
                return b
            if b.start > start:
                if i == 0 and len(q) >= self.buckets:
                    return Bucket(start)
                q.insert(i, Bucket(start))
                return q[i]
        return Bucket(start)

    def _heard(self, d: Decoded) -> None:
        f = d.frame
        call = str(f.source)
        h = self.heard.pop(call, None) or HeardStation(call, d.time, d.time)
        h.last = max(h.last, d.time)
        h.frames += 1
        h.channels[d.channel] += 1
        if f.digipeated:
            h.via[f.heard_from] += 1
        else:
            h.direct += 1
            h.level_db = d.level_db
        h.last_text = f.monitor()
        self.heard[call] = h
        while len(self.heard) > self.heard_max:
            self.heard.popitem(last=False)
        # A digipeater that repeated the frame was heard too: its own
        # transmitter, at the level measured.
        if f.digipeated:
            digi = f.heard_from
            g = self.heard.pop(digi, None) or HeardStation(digi, d.time, d.time)
            g.last = max(g.last, d.time)
            g.channels[d.channel] += 1
            g.direct += 1
            g.level_db = d.level_db
            self.heard[digi] = g

    def _node(self, call: str, d: Decoded) -> Node:
        n = self.nodes.get(call)
        if n is None:
            n = self.nodes[call] = Node(call, d.time, d.time)
        n.last = max(n.last, d.time)
        n.channels[d.channel] += 1
        return n

    def _roles(self, d: Decoded) -> None:
        f = d.frame
        src = str(f.source)
        t = f.type
        if d.nodes is not None:
            n = self._node(src, d)
            n.roles.add("netrom-node")
            n.alias = d.nodes.alias
            n.routes = list(d.nodes.routes)
            return
        if t in CONNECTED:
            n = self._node(src, d)
            n.roles.add("connected")
            if t is FrameType.UA:
                n.roles.add("answers")
            if t is FrameType.I and f.pid == PID_NETROM:
                n.roles.add("netrom")
        elif t is FrameType.UI and (_BBS_WORD.search(f.text()) or f.destination.call in ("BBS", "MAIL")):
            if f.pid in (PID_NONE, None):
                self._node(src, d).roles.add("bbs")

    def _links(self, d: Decoded) -> None:
        f = d.frame
        src, dst = str(f.source), str(f.destination)
        kind = None
        if f.type in CONNECTED:
            kind = "ax25"
        elif d.aprs is not None and d.aprs.kind == "message" and d.aprs.addressee:
            kind, dst = "aprs-message", d.aprs.addressee.strip().upper()
        if kind is None or src == dst:
            return
        a, b = sorted((src, dst))
        key = (a, b, kind)
        link = self.links.get(key)
        if link is None:
            link = self.links[key] = Link(a, b, kind, d.time, d.time)
        link.last = max(link.last, d.time)
        if src == a:
            link.a_to_b += 1
        else:
            link.b_to_a += 1
        link.channels[d.channel] += 1
        link.types[f.type.value] += 1

    def _position(self, d: Decoded) -> None:
        p = d.aprs
        if p is None or p.position is None:
            return
        src = str(d.frame.source)
        name = p.name.strip() if p.kind in ("object", "item") and p.name else src
        pos = self.positions.get(name)
        if pos is None:
            pos = self.positions[name] = PositionReport(name, p.position.lat, p.position.lon, d.time, d.channel)
        elif d.time < pos.time:
            return
        pos.lat, pos.lon, pos.time, pos.channel = p.position.lat, p.position.lon, d.time, d.channel
        pos.symbol, pos.comment, pos.source = p.position.symbol, p.comment, src
        if not pos.track or pos.track[-1][1:] != (pos.lat, pos.lon):
            pos.track.append((d.time, pos.lat, pos.lon))
            del pos.track[:-TRACK_POINTS]

    # -- out to the app -------------------------------------------------------

    def ppm_estimate(self, minimum: int = 3) -> float | None:
        """How many ppm to add to the channeliser's crystal setting, from
        the frames heard (module docstring); None until `minimum` strong
        frames have been heard. A station read 430 Hz high on 144.8 MHz
        says the receiver's crystal is 3 ppm slower than it was told."""
        with self.lock:
            vals = sorted(-off / hz * 1e6 for hz, off in self._offsets)
        if len(vals) < minimum:
            return None
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2

    def feed_heard(self, station) -> None:
        """Every frame the SDR hears into an AprsStation's heard table
        (AprsStation.hear: the table only; nothing heard here is answered)."""
        self.listeners.append(lambda d: d.frame is not None and station.hear(d.frame, d.aprs))

    def snapshot(self, frames: int = 200) -> dict:
        """Everything, as plain dicts and lists (JSON-ready but for bytes)."""
        with self.lock:
            return {
                "rev": self.rev,
                "frames": [d.as_dict() for d in list(self.frames)[-frames:]],
                "heard": [{"call": h.call, "first": h.first, "last": h.last, "frames": h.frames,
                           "channels": dict(h.channels), "direct": h.direct, "via": dict(h.via),
                           "levelDb": h.level_db, "lastText": h.last_text} for h in self.heard.values()],
                "nodes": [{"call": n.call, "first": n.first, "last": n.last, "roles": sorted(n.roles),
                           "alias": n.alias, "channels": dict(n.channels),
                           "routes": [r.__dict__ for r in n.routes]} for n in self.nodes.values()],
                "links": [{"a": k.a, "b": k.b, "kind": k.kind, "first": k.first, "last": k.last,
                           "aToB": k.a_to_b, "bToA": k.b_to_a, "channels": dict(k.channels),
                           "types": dict(k.types)} for k in self.links.values()],
                "positions": [{"name": p.name, "lat": p.lat, "lon": p.lon, "time": p.time, "channel": p.channel,
                               "symbol": p.symbol, "comment": p.comment, "source": p.source,
                               "track": list(p.track)} for p in self.positions.values()],
                "channels": [{"id": c.id, "hz": c.hz, "label": c.label, "kind": c.kind,
                              "levelDb": self.channels[c.id].level_db, "floorDb": self.channels[c.id].floor_db,
                              "frames": self.channels[c.id].frames, "fx25": self.channels[c.id].fx25,
                              "activity": [b.__dict__.copy() for b in self.channels[c.id].activity]}
                             for c in self.plan],
                "ppm": self.ppm_estimate(),
                "source": dict(self.source_status),
            }
