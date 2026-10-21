"""NET/ROM's routing broadcast, read (not sent): who is a node, and what it
can reach.

A NET/ROM node (and TheNet, BPQ, XRouter and every other node that talks
NET/ROM) sends a UI frame to the destination NODES with PID 0xCF every
half hour or so. Its information field is 0xFF, the sending node's
six-character alias (mnemonic), and then up to eleven 21-byte routes: the
destination node's callsign (an AX.25 address), its alias, the neighbour
the route goes through (another address) and the route's quality 0-255.
(The NET/ROM documentation of 1987, as every implementation since reads
it; direwolf, linux's netrom and BPQ agree.)

The SDR monitor uses this to say which stations on a channel are nodes and
what they advertise; release 2's own node will build on it. A broadcast
that does not hold together (an address that is not one, a short route)
is reported as far as it was good, never raised: a stranger's frame must
not be able to stop the monitor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ax25 import PID_NETROM, Address, Frame, FrameType, decode_address

SIGNATURE = 0xFF
ROUTE_BYTES = 21


@dataclass(frozen=True)
class Route:
    destination: str
    alias: str
    neighbour: str
    quality: int


@dataclass
class NodesBroadcast:
    node: str                 # who sent it
    alias: str
    routes: list[Route] = field(default_factory=list)
    truncated: bool = False   # it ended part-way through a route, or a route did not parse


def _alias(raw: bytes) -> str:
    return raw.decode("latin-1").strip(" \x00")


def parse_nodes(frame: Frame) -> NodesBroadcast | None:
    """The broadcast this frame is, or None if it is not one."""
    if frame.type is not FrameType.UI or frame.pid != PID_NETROM or frame.destination.call != "NODES":
        return None
    info = frame.info
    if not info or info[0] != SIGNATURE or len(info) < 7:
        return None
    out = NodesBroadcast(str(frame.source), _alias(info[1:7]))
    body = info[7:]
    whole = len(body) // ROUTE_BYTES * ROUTE_BYTES
    out.truncated = whole != len(body)
    for i in range(0, whole, ROUTE_BYTES):
        r = body[i:i + ROUTE_BYTES]
        dest, neigh = decode_address(r[0:7]), decode_address(r[13:20])
        if dest is None or neigh is None:
            out.truncated = True
            break
        out.routes.append(Route(str(dest), _alias(r[7:13]), str(neigh), r[20]))
    return out


def build_nodes(node: str, alias: str, routes=()) -> Frame:
    """A broadcast as a node would send it: for tests and the simulator,
    never transmitted from here (this module has no radio)."""
    def addr(call: str) -> bytes:
        return Address.from_text(call).encode(last=False)
    info = bytes([SIGNATURE]) + alias.upper().ljust(6)[:6].encode("ascii")
    for r in routes:
        info += addr(r.destination) + r.alias.upper().ljust(6)[:6].encode("ascii") + addr(r.neighbour)
        info += bytes([r.quality & 0xFF])
    return Frame.ui(node, "NODES", info, pid=PID_NETROM)


#: The quality a NET/ROM node gives a neighbour it hears directly on a
#: 1200-baud port unless told otherwise (BPQ's QUALITY default, TheNet's
#: too). A route's quality as this station would see it is the quality
#: advertised times this, over 256: the same sum a node does on every
#: broadcast it hears.
DEFAULT_PATH_QUALITY = 192


@dataclass
class KnownNode:
    """One node this station knows of: heard broadcasting, or named in a
    broadcast or a NODES/ROUTES listing. `neighbour` is the best way in
    (the node to connect to first); "" when it was heard directly, so it
    can be connected to as it is."""
    call: str
    alias: str = ""
    quality: int = 0
    neighbour: str = ""
    heard: float = 0.0           # when last told of it (the caller's clock)
    source: str = "air"          # "air" (a broadcast), "nodes" or "routes" (a listing)
    direct: bool = False         # its own broadcast was heard


class NodeTable:
    """The node browser's table: every NODES broadcast heard and every
    NODES or ROUTES listing a node printed, merged by callsign.

    A broadcaster is a node heard directly (`direct`, quality
    DEFAULT_PATH_QUALITY, no neighbour). Each route it advertises becomes a
    node reached through it, at the derived quality; a better route
    replaces a worse one, and a route through the same neighbour is
    refreshed whatever its quality now is, as NET/ROM does. A listing adds
    what a node we are connected to knows, reached through that node.
    Quality 0 routes are kept (they say the node exists) but rank last.
    """

    def __init__(self, path_quality: int = DEFAULT_PATH_QUALITY):
        self.path_quality = path_quality
        self.nodes: dict[str, KnownNode] = {}
        self.rev = 0

    def _offer(self, call: str, alias: str, quality: int, neighbour: str, now: float, source: str,
               direct: bool = False) -> None:
        call = call.upper()
        old = self.nodes.get(call)
        if old is None:
            self.nodes[call] = KnownNode(call, alias, quality, neighbour, now, source, direct)
        else:
            if direct:
                old.direct, old.neighbour, old.quality, old.source = True, "", max(old.quality, quality), source
            elif not old.direct and (quality >= old.quality or neighbour == old.neighbour):
                old.quality, old.neighbour, old.source = quality, neighbour, source
            if alias:
                old.alias = alias
            old.heard = now
        self.rev += 1

    def heard_broadcast(self, b: NodesBroadcast, now: float) -> None:
        self._offer(b.node, b.alias, self.path_quality, "", now, "air", direct=True)
        for r in b.routes:
            if r.destination.upper() == b.node.upper():
                continue
            q = r.quality * self.path_quality // 256
            self._offer(r.destination, r.alias, q, b.node, now, "air")

    def heard_frame(self, frame: Frame, now: float) -> bool:
        """A frame off the air; True if it was a NODES broadcast."""
        b = parse_nodes(frame)
        if b is None:
            return False
        self.heard_broadcast(b, now)
        return True

    def listed(self, source: str, nodes=(), routes=(), now: float = 0.0) -> None:
        """A node's NODES listing (terminal.NodeEntry: alias, call) or
        ROUTES listing (terminal.RouteEntry), as printed by `source`."""
        for n in nodes:
            if n.call.upper() != source.upper():
                self._offer(n.call, n.alias, 0, source, now, "nodes")
        for r in routes:
            if r.call.upper() != source.upper():
                self._offer(r.call, "", r.quality, source, now, "routes")

    def ranked(self) -> list[KnownNode]:
        """Best first: heard directly, then by quality, then by call."""
        return sorted(self.nodes.values(), key=lambda n: (not n.direct, -n.quality, n.call))

    def find(self, text: str) -> KnownNode | None:
        """A node by call or alias, as an operator types either."""
        t = str(text or "").strip().upper()
        return self.nodes.get(t) or next((n for n in self.nodes.values() if n.alias and n.alias == t), None)
