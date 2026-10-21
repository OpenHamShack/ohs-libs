"""IP over AX.25 (RFC 1226), with ARP over AX.25 (RFC 826, hardware type
3) and a route table.

The engine's side is "IP packet in, IP packet out": `IpInterface.send`
takes an IPv4 packet from the host, `on_packet` hands one up. Making a
TUN (Linux), utun (macOS) or Wintun (Windows) interface to connect that to
the operating system's stack is the app's, later, and so is any address
configuration; with none, `answer_ping` lets a node with no stack at all
answer an ICMP echo itself.

**Two modes, per destination** (RFC 1226 §2):

- *datagram* - each packet one UI frame, PID 0xCC, to the next hop's
  callsign. No acknowledgement: TCP above does the retrying. This is the
  default, and what Linux and JNOS do unless told otherwise.
- *virtual circuit* - an AX.25 connection to the next hop, packets in I
  frames with PID 0xCC. The link layer retries, which is better on a lossy
  channel than TCP's slow back-off. A packet longer than N1 is cut by the
  AX.25 2.2 segmenter (engine.Connection.send_packet), so the MTU can be
  above the frame size. A connection is opened on first use and kept;
  incoming ones are accepted from stations with an ARP entry or route in
  virtual-circuit mode (`accept_vc`: "known", "any" or "none").

**ARP** (PID 0xCD): to reach an IP address with no entry, the packet waits
while a request goes to QST (Linux's broadcast address for AX.25), retried
`arp_tries` times; a reply - or a request from that station - fills the
entry, with the path the reply came by, reversed. Static entries never
expire. This station answers requests for its own address.

**Routing**: the longest matching prefix in `routes` gives the next hop (a
gateway, or the destination itself on a directly reachable network) and
may force the mode. Nothing is forwarded between stations: the engine is a
host, not a router.

**Every packet passes the encryption guard** (guard.py) before anything is
queued; a refusal is counted, logged and reported as an "ip-refused" event
with the guard's key, and nothing is transmitted.
"""

from __future__ import annotations

import collections
import ipaddress
import logging
import struct
from dataclasses import dataclass, field
from typing import Callable

from .ax25 import PID_ARP, PID_IP, Address, Frame, decode_address
from .engine import Connection, Event, IncomingCall, in_transit
from .guard import EncryptionGuard, GuardConfig

log = logging.getLogger(__name__)

#: Refusals of the IP layer itself (the guard's are guard.REFUSALS).
REFUSALS = {
    "no-route": "no route to that address",
    "arp-failed": "no station answered for that address (ARP)",
    "too-big": "longer than the interface's MTU in datagram mode",
    "queue-full": "too many packets waiting for one address",
}

ARP_HARDWARE_AX25 = 3
ARP_PROTOCOL_IP = 0x0800
ARP_REQUEST, ARP_REPLY = 1, 2


@dataclass
class Route:
    network: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address | None = None
    mode: str | None = None


@dataclass
class ArpEntry:
    ip: ipaddress.IPv4Address
    call: Address
    via: list[Address] = field(default_factory=list)
    mode: str | None = None
    expires: float | None = None          # None: static


@dataclass
class IpConfig:
    address: str
    #: The network reached directly (on this channel), e.g. 44.131.0.0/16.
    network: str | None = None
    mode: str = "datagram"
    mtu: int = 256
    arp_ttl_s: float = 900.0
    arp_retry_s: float = 10.0
    arp_tries: int = 3
    arp_broadcast: str = "QST"
    #: Digipeaters for ARP requests and for addresses learned without a path.
    via: list[str] = field(default_factory=list)
    queue_max: int = 16
    accept_vc: str = "known"
    answer_ping: bool = False
    guard: GuardConfig = field(default_factory=GuardConfig)


def _call(value) -> Address:
    if isinstance(value, Address):
        return Address(value.call, value.ssid)
    return Address.from_text(str(value), flag=False)


def ipv4_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


class IpInterface:
    def __init__(self, engine, config: IpConfig, guard: EncryptionGuard | None = None):
        self.engine = engine
        self.config = config
        self.address = ipaddress.IPv4Address(config.address)
        self.guard = guard or EncryptionGuard(config.guard, clock=engine.clock)
        self.routes: list[Route] = []
        if config.network:
            self.routes.append(Route(ipaddress.IPv4Network(config.network, strict=False)))
        self.arp: dict[ipaddress.IPv4Address, ArpEntry] = {}
        self._waiting: dict[ipaddress.IPv4Address, collections.deque] = {}
        self._asking: dict[ipaddress.IPv4Address, tuple[int, float]] = {}
        self._circuits: dict[str, Connection] = {}
        self._circuit_queue: dict[str, collections.deque] = {}
        self.on_packet: Callable[[bytes], None] | None = None
        self.stats = collections.Counter()
        engine.ui_handlers.append(self._ui)
        engine.packet_handlers[PID_IP] = self._from_circuit
        engine.listen(engine.station.address(), self._accept)
        engine.services.append(self)

    # -- configuration ------------------------------------------------------------

    def add_route(self, network: str, gateway: str | None = None, mode: str | None = None) -> None:
        self.routes.append(Route(ipaddress.IPv4Network(network, strict=False),
                                 ipaddress.IPv4Address(gateway) if gateway else None, mode))

    def add_arp(self, ip: str, call, via=(), mode: str | None = None) -> None:
        """A static entry: never expires, never replaced by what is heard."""
        address = ipaddress.IPv4Address(ip)
        self.arp[address] = ArpEntry(address, _call(call), [_call(v) for v in via], mode, None)

    # -- out -------------------------------------------------------------------------

    def _refuse(self, reason: str, packet: bytes) -> bool:
        self.stats["refused_" + reason] += 1
        self.engine._emit(Event("ip-refused", reason, detail={"length": len(packet)}))
        return False

    def send(self, packet: bytes) -> bool:
        """Send an IPv4 packet. False if it was refused (see the events);
        True if it was sent or is waiting for ARP or a connection. Safe from
        any thread (the TUN reader's)."""
        with self.engine.lock:
            return self._send(bytes(packet))

    def _send(self, packet: bytes) -> bool:
        reason = self.guard.check(packet)
        if reason is not None:
            return self._refuse(reason, packet)
        dst = ipaddress.IPv4Address(packet[16:20])
        hop, mode = self._route(dst)
        if hop is None:
            return self._refuse("no-route", packet)
        entry = self.arp.get(hop)
        if entry is not None and entry.expires is not None and entry.expires < self.engine.clock():
            del self.arp[hop]
            entry = None
        if entry is None:
            queue = self._waiting.setdefault(hop, collections.deque())
            if len(queue) >= self.config.queue_max:
                return self._refuse("queue-full", packet)
            queue.append((packet, mode))
            if hop not in self._asking:
                self._ask(hop)
            return True
        return self._transmit(packet, entry, mode)

    def _route(self, dst: ipaddress.IPv4Address):
        best = None
        for route in self.routes:
            if dst in route.network and (best is None or route.network.prefixlen > best.network.prefixlen):
                best = route
        if best is None:
            return None, None
        return (best.gateway or dst), best.mode

    def _transmit(self, packet: bytes, entry: ArpEntry, mode: str | None) -> bool:
        mode = entry.mode or mode or self.config.mode
        if mode == "vc":
            return self._via_circuit(packet, entry)
        if len(packet) > self.config.mtu:
            return self._refuse("too-big", packet)
        frame = Frame.ui(self.engine.station.address(), entry.call, packet, via=entry.via, pid=PID_IP)
        self.engine.send_frame(frame)
        self.stats["sent_datagram"] += 1
        return True

    def _via_circuit(self, packet: bytes, entry: ArpEntry) -> bool:
        key = str(entry.call)
        conn = self._circuits.get(key)
        if conn is None or conn.state.value == "disconnected":
            conn = self.engine.connect(entry.call, via=entry.via)
            self._circuits[key] = conn
        if not conn.connected:
            queue = self._circuit_queue.setdefault(key, collections.deque())
            if len(queue) >= self.config.queue_max:
                return self._refuse("queue-full", packet)
            queue.append(packet)
            return True
        conn.send_packet(PID_IP, packet)
        self.stats["sent_circuit"] += 1
        return True

    # -- ARP -------------------------------------------------------------------------

    def _arp_frame(self, op: int, target_call: Address | None, target_ip: ipaddress.IPv4Address) -> bytes:
        me = self.engine.station.address()
        hw_me = me.encode(last=False)
        hw_target = target_call.encode(last=False) if target_call is not None else bytes(7)
        return struct.pack(">HHBBH", ARP_HARDWARE_AX25, ARP_PROTOCOL_IP, 7, 4, op) + hw_me + \
            self.address.packed + hw_target + target_ip.packed

    def _ask(self, ip: ipaddress.IPv4Address) -> None:
        tries = self._asking.get(ip, (0, 0.0))[0]
        if tries >= self.config.arp_tries:
            self._asking.pop(ip, None)
            dropped = self._waiting.pop(ip, collections.deque())
            self.stats["arp_failed"] += 1
            for packet, _ in dropped:
                self._refuse("arp-failed", packet)
            return
        now = self.engine.clock()
        self._asking[ip] = (tries + 1, now + self.config.arp_retry_s)
        frame = Frame.ui(self.engine.station.address(), _call(self.config.arp_broadcast),
                         self._arp_frame(ARP_REQUEST, None, ip), via=[_call(v) for v in self.config.via],
                         pid=PID_ARP)
        self.engine.send_frame(frame)
        self.stats["arp_requests"] += 1

    def _learn(self, ip: ipaddress.IPv4Address, call: Address, via: list[Address]) -> None:
        old = self.arp.get(ip)
        if old is not None and old.expires is None:
            return                                # static entries are the operator's
        mode = old.mode if old is not None else None
        self.arp[ip] = ArpEntry(ip, call, via, mode, self.engine.clock() + self.config.arp_ttl_s)
        self._asking.pop(ip, None)
        for packet, route_mode in self._waiting.pop(ip, collections.deque()):
            self._transmit(packet, self.arp[ip], route_mode)

    def _arp_in(self, frame: Frame) -> None:
        data = frame.info
        if len(data) < 28:
            return
        hrd, pro, hln, pln, op = struct.unpack(">HHBBH", data[:8])
        if hrd != ARP_HARDWARE_AX25 or pro != ARP_PROTOCOL_IP or hln != 7 or pln != 4:
            return
        sender = decode_address(data[8:15])
        if sender is None:
            return
        sender = Address(sender.call, sender.ssid)
        sender_ip = ipaddress.IPv4Address(data[15:19])
        target_ip = ipaddress.IPv4Address(data[26:30]) if len(data) >= 30 else None
        path = [Address(h.call, h.ssid) for h in reversed(frame.via)]
        self.stats["arp_heard"] += 1
        if sender_ip in self.arp or target_ip == self.address or sender_ip in self._asking:
            self._learn(sender_ip, sender, path)
        if op == ARP_REQUEST and target_ip == self.address:
            reply = Frame.ui(self.engine.station.address(), sender,
                             self._arp_frame(ARP_REPLY, sender, sender_ip), via=path, pid=PID_ARP)
            self.engine.send_frame(reply)
            self.stats["arp_replies"] += 1

    # -- in ----------------------------------------------------------------------------

    def _ui(self, frame: Frame) -> None:
        if in_transit(frame):
            return
        if frame.pid == PID_ARP:
            self._arp_in(frame)
            return
        if frame.pid != PID_IP:
            return
        me = self.engine.station.address()
        if frame.destination.call != me.call or frame.destination.ssid != me.ssid:
            return
        self.stats["received_datagram"] += 1
        self._deliver(frame.info)

    def _from_circuit(self, conn: Connection, pid: int, data: bytes) -> None:
        self._circuits.setdefault(str(conn.remote), conn)
        self.stats["received_circuit"] += 1
        self._deliver(data)

    def _accept(self, call: IncomingCall):
        policy = self.config.accept_vc
        if policy == "none":
            return None
        if policy == "known":
            known = any(e.call.call == call.remote.call and e.call.ssid == call.remote.ssid and
                        (e.mode or self.config.mode) == "vc" for e in self.arp.values())
            if not known:
                return None

        def setup(conn):
            self._circuits[str(conn.remote)] = conn
        return setup

    def _deliver(self, packet: bytes) -> None:
        if self.config.answer_ping and self._ping_reply(packet):
            return
        if self.on_packet is not None:
            self.on_packet(packet)

    def _ping_reply(self, packet: bytes) -> bool:
        """An ICMP echo request to our address, answered here (answer_ping)."""
        if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != 1:
            return False
        ihl = (packet[0] & 0x0F) * 4
        if ipaddress.IPv4Address(packet[16:20]) != self.address or packet[ihl] != 8:
            return False
        icmp = bytearray(packet[ihl:])
        icmp[0] = 0
        icmp[2:4] = b"\0\0"
        icmp[2:4] = struct.pack(">H", ipv4_checksum(bytes(icmp)))
        header = bytearray(packet[:ihl])
        header[8] = 64
        header[12:16], header[16:20] = packet[16:20], packet[12:16]
        header[10:12] = b"\0\0"
        header[10:12] = struct.pack(">H", ipv4_checksum(bytes(header)))
        self.stats["pings_answered"] += 1
        self.send(bytes(header) + bytes(icmp))
        return True

    # -- time ------------------------------------------------------------------------

    def poll(self, now: float) -> None:
        for ip, (tries, due) in list(self._asking.items()):
            if now + 1e-9 >= due:
                self._ask(ip)
        for key, queue in list(self._circuit_queue.items()):
            conn = self._circuits.get(key)
            if conn is None:
                continue
            if conn.connected:
                while queue:
                    conn.send_packet(PID_IP, queue.popleft())
                    self.stats["sent_circuit"] += 1
            elif conn.state.value == "disconnected":
                for packet in queue:
                    self._refuse("no-route", packet)
                queue.clear()

    def next_deadline(self) -> float | None:
        due = [d for _, d in self._asking.values()]
        if any(q and self._circuits.get(k) is not None and self._circuits[k].connected
               for k, q in self._circuit_queue.items()):
            due.append(self.engine.clock())
        return min(due) if due else None
