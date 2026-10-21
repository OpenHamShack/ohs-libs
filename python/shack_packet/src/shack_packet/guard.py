"""The encryption guard: refuse to transmit IP traffic that is evidently
encrypted.

A UK amateur licence forbids messages "encrypted for the purpose of
rendering the Message unintelligible to other radio spectrum users"
(Ofcom, the Amateur Radio Licence, clause 11(2)); most administrations say
the same. A TUN interface hands the engine whatever the operator's
computer sends, and a modern computer sends TLS, QUIC and VPN traffic all
the time without being asked, so the engine checks every IP packet before
it can be keyed and refuses what is plainly encrypted. Refusals are
counted by reason (`refused`), logged, and reported to the app as events
with the keys in REFUSALS, so the operator can see what was stopped and
why. It is a policy hook too: `extra` checks run after the built-in ones,
and the app can add its own.

**What it catches** (each by structure, on any port, and by a port
deny-list besides):

- TLS records (handshake, application data, alerts: content type 20-23,
  version 3.x, a plausible length) at the start of a TCP segment, which is
  where a ClientHello always is - including after STARTTLS on SMTP, IMAP or
  XMPP; DTLS the same over UDP.
- QUIC: every connection starts with long-header packets (top two bits
  set, version 1, 2 or a draft), which are refused, so no QUIC connection
  ever gets going.
- SSH: the version banner ("SSH-") that opens every session.
- IPsec: ESP (IP protocol 50), ESP and IKE in UDP 4500 (NAT traversal),
  IKEv1 and IKEv2 headers on any UDP port. AH (51) only authenticates and
  hides nothing, so it is allowed.
- WireGuard's four message types by their fixed shape (type, three zero
  bytes, fixed lengths).
- OpenVPN's hard-reset opcodes that open every session, over UDP and TCP.
- Tunnels: GRE carrying IPv4, and IP-in-IP, are opened and the inner packet
  checked the same way.
- Every later packet of a TCP or UDP flow (both directions) one of these
  refused, for `flow_ttl_s`, and every later fragment of a refused
  datagram - a handshake that was stopped means its connection never forms,
  and this makes sure.
- A deny-list of ports whose services are encrypted by definition (HTTPS,
  SSH, IMAPS, POP3S, SMTPS, DNS over TLS/QUIC, OpenVPN, WireGuard, IKE).
- Anything that is not IPv4: IPv6 is not carried at all (RFC 1226 and ARP
  over AX.25 are IPv4), so nothing can pass by being harder to parse.

**What it cannot catch**, honestly: encryption inside a protocol it lets
through - a PGP or age message in plain SMTP or HTTP, an encrypted ZIP or
document over FTP, a password-protected archive attached to anything; a
cipher or VPN built to look like something else (obfs4, Shadowsocks, a VPN
inside plain HTTP or DNS); a new protocol it has no signature for on a port
not in the deny-list; a flow whose opening packet it never saw (the flow
table is in memory and expires); the later fragments of a datagram whose
first fragment it did not see. It does not judge payloads by randomness:
compressed data (images, gzip, video) looks exactly like ciphertext, and
refusing it would refuse half of ordinary traffic while missing nothing a
determined user could not compress first. The guard stops the operator's
computer from encrypting by accident; it cannot stop an operator who means
to, and the licence obligation remains the operator's.
"""

from __future__ import annotations

import collections
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: Why a packet was refused, as keys the app translates.
REFUSALS = {
    "not-ipv4": "only IPv4 is carried over AX.25",
    "malformed": "not a whole IPv4 packet",
    "tls": "a TLS record (encrypted web, mail or other TLS traffic)",
    "dtls": "a DTLS record (TLS over UDP)",
    "quic": "a QUIC packet (encrypted, as used by HTTP/3)",
    "ssh": "an SSH session (encrypted)",
    "ipsec-esp": "IPsec ESP (encrypted)",
    "ike": "IPsec key exchange (IKE), which sets up encryption",
    "wireguard": "a WireGuard VPN packet (encrypted)",
    "openvpn": "an OpenVPN session (encrypted)",
    "denied-port": "a port whose service is encrypted (the deny-list)",
    "encrypted-flow": "part of a connection already refused as encrypted",
    "fragment-of-refused": "a fragment of a packet already refused",
    "policy": "refused by the station's own policy",
}

#: Services encrypted by definition. TCP: SSH, HTTPS, SMTPS, NNTPS, LDAPS,
#: DNS over TLS, FTPS, TELNETS, IMAPS, IRCS, POP3S, OpenVPN, SIP-TLS,
#: alternative HTTPS. UDP: QUIC/HTTP3, IKE, DNS over QUIC, OpenVPN, IKE NAT
#: traversal, WireGuard.
DENY_TCP = frozenset({22, 443, 465, 563, 636, 853, 989, 990, 992, 993, 994, 995, 1194, 5061, 8443})
DENY_UDP = frozenset({443, 500, 853, 1194, 4500, 51820})

_QUIC_VERSIONS = {0x00000001, 0x6B3343CF}
_OPENVPN_RESETS = {7, 8, 10}      # P_CONTROL_HARD_RESET_CLIENT_V2, _SERVER_V2, _CLIENT_V3


@dataclass
class GuardConfig:
    enabled: bool = True
    deny_tcp: frozenset = DENY_TCP
    deny_udp: frozenset = DENY_UDP
    deny_protocols: frozenset = frozenset({50})
    flow_ttl_s: float = 600.0
    #: More checks: (Packet) -> REFUSALS key or None, after the built-in ones.
    extra: list[Callable[[Packet], str | None]] = field(default_factory=list)


@dataclass
class Packet:
    """An IPv4 packet taken apart as far as the guard needs."""
    src: bytes
    dst: bytes
    proto: int
    ident: int
    offset: int
    more_fragments: bool
    payload: bytes
    sport: int | None = None
    dport: int | None = None
    data: bytes = b""

    @property
    def flow(self) -> tuple | None:
        if self.sport is None:
            return None
        a, b = (self.src, self.sport), (self.dst, self.dport)
        return (self.proto,) + (a + b if a <= b else b + a)

    @property
    def datagram(self) -> tuple:
        return (self.src, self.dst, self.ident, self.proto)


def parse_ipv4(packet: bytes) -> Packet | None:
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    total = struct.unpack(">H", packet[2:4])[0]
    if ihl < 20 or total < ihl or total > len(packet):
        return None
    ident = struct.unpack(">H", packet[4:6])[0]
    flags_offset = struct.unpack(">H", packet[6:8])[0]
    out = Packet(packet[12:16], packet[16:20], packet[9], ident, (flags_offset & 0x1FFF) * 8,
                 bool(flags_offset & 0x2000), packet[ihl:total])
    if out.offset == 0 and out.proto in (6, 17):
        p = out.payload
        if len(p) >= 4:
            out.sport, out.dport = struct.unpack(">HH", p[:4])
        if out.proto == 6 and len(p) >= 20:
            out.data = p[(p[12] >> 4) * 4:]
        elif out.proto == 17 and len(p) >= 8:
            out.data = p[8:]
    return out


def _tls_record(data: bytes) -> bool:
    if len(data) < 5 or not 20 <= data[0] <= 23 or data[1] != 3 or data[2] > 4:
        return False
    length = struct.unpack(">H", data[3:5])[0]
    return 0 < length <= 16384 + 2048


def _dtls_record(data: bytes) -> bool:
    return len(data) >= 13 and 20 <= data[0] <= 23 and data[1] == 0xFE and data[2] in (0xFF, 0xFD, 0xFC)


def _quic_long(data: bytes) -> bool:
    if len(data) < 5 or data[0] & 0xC0 != 0xC0:
        return False
    version = struct.unpack(">I", data[1:5])[0]
    return version in _QUIC_VERSIONS or version >> 8 == 0xFF0000


def _ike(data: bytes) -> bool:
    if len(data) < 28:
        return False
    version, exchange = data[17], data[18]
    length = struct.unpack(">I", data[24:28])[0]
    if length != len(data):
        return False
    return (version == 0x20 and 34 <= exchange <= 37) or (version == 0x10 and exchange in (1, 2, 4, 5, 6, 32))


def _wireguard(data: bytes) -> bool:
    if len(data) < 32 or data[1:4] != b"\0\0\0":
        return False
    kind = data[0]
    return (kind == 1 and len(data) == 148) or (kind == 2 and len(data) == 92) or \
        (kind == 3 and len(data) == 64) or (kind == 4 and len(data) % 16 == 0)


def _openvpn(data: bytes, on_port: bool) -> bool:
    """A hard reset, the packet that opens every OpenVPN session: its
    opcode with key id 0, and - off its own port - the message packet id
    0 that ends it. The opcode alone is one byte, and a DNS query's random
    ID matched it one time in 85; the trailing zeros do not
    occur in DNS (which ends in its query type and class)."""
    if len(data) < 14 or data[0] >> 3 not in _OPENVPN_RESETS or data[0] & 0x07:
        return False
    return on_port or data[-4:] == b"\0\0\0\0"


class EncryptionGuard:
    def __init__(self, config: GuardConfig | None = None, clock: Callable[[], float] | None = None):
        self.config = config or GuardConfig()
        self.clock = clock or time.monotonic
        self.refused: collections.Counter = collections.Counter()
        self.passed = 0
        self._flows: dict[tuple, float] = {}
        self._datagrams: dict[tuple, float] = {}

    def check(self, packet: bytes) -> str | None:
        """None if `packet` may be sent, else the REFUSALS key."""
        if not self.config.enabled:
            self.passed += 1
            return None
        now = self.clock()
        reason = self._check(packet, now, depth=0)
        if reason is None:
            self.passed += 1
            return None
        self.refused[reason] += 1
        log.info("IP packet refused by the encryption guard: %s", reason)
        return reason

    def _remember(self, parsed: Packet, now: float) -> None:
        until = now + self.config.flow_ttl_s
        if parsed.flow is not None:
            self._flows[parsed.flow] = until
        if parsed.more_fragments or parsed.offset:
            self._datagrams[parsed.datagram] = until
        if len(self._flows) > 4096 or len(self._datagrams) > 4096:
            self._flows = {k: v for k, v in self._flows.items() if v > now}
            self._datagrams = {k: v for k, v in self._datagrams.items() if v > now}

    def _check(self, packet: bytes, now: float, depth: int) -> str | None:
        if not packet or packet[0] >> 4 != 4:
            return "not-ipv4"
        parsed = parse_ipv4(packet)
        if parsed is None:
            return "malformed"
        cfg = self.config
        if self._datagrams.get(parsed.datagram, 0) > now:
            return "fragment-of-refused"
        if parsed.flow is not None and self._flows.get(parsed.flow, 0) > now:
            return "encrypted-flow"
        reason = self._judge(parsed, now, depth)
        if reason is None:
            for extra in cfg.extra:
                reason = extra(parsed)
                if reason:
                    break
        if reason is not None:
            self._remember(parsed, now)
        return reason

    def _judge(self, p: Packet, now: float, depth: int) -> str | None:
        cfg = self.config
        if p.proto == 50:
            return "ipsec-esp"
        if p.proto in cfg.deny_protocols:
            return "policy"
        if p.offset:
            return None                      # a later fragment: nothing to see (see above)
        if p.proto == 4 and depth < 3:       # IP in IP
            return self._check(p.payload, now, depth + 1)
        if p.proto == 47 and depth < 3 and len(p.payload) >= 4:
            flags, ethertype = struct.unpack(">HH", p.payload[:4])
            if ethertype == 0x0800:
                skip = 4 + (4 if flags & 0x8000 else 0) + (4 if flags & 0x2000 else 0) + (4 if flags & 0x1000 else 0)
                return self._check(p.payload[skip:], now, depth + 1)
            return None
        data = p.data
        if p.proto == 6:
            if data.startswith(b"SSH-"):
                return "ssh"
            if _tls_record(data):
                return "tls"
            if len(data) >= 16 and struct.unpack(">H", data[:2])[0] == len(data) - 2 and \
                    _openvpn(data[2:], 1194 in (p.sport, p.dport)):
                return "openvpn"
            if p.sport in cfg.deny_tcp or p.dport in cfg.deny_tcp:
                return "denied-port"
        elif p.proto == 17:
            if 4500 in (p.sport, p.dport) and len(data) >= 8:
                # NAT traversal: four zero bytes, then IKE; else ESP in UDP.
                return "ike" if data[:4] == b"\0\0\0\0" else "ipsec-esp"
            if _dtls_record(data):
                return "dtls"
            if _quic_long(data):
                return "quic"
            if _ike(data):
                return "ike"
            if _wireguard(data):
                return "wireguard"
            if _openvpn(data, 1194 in (p.sport, p.dport)):
                return "openvpn"
            if p.sport in cfg.deny_udp or p.dport in cfg.deny_udp:
                return "denied-port"
        return None
