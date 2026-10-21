"""The encryption guard on packets built by hand: what it refuses, what it
lets through, and that ordinary traffic is not caught by accident."""

from __future__ import annotations

import random
import struct

import pytest
from ippackets import TLS_CLIENT_HELLO, icmp_echo, ipv4, tcp, udp

from shack_packet.guard import REFUSALS, EncryptionGuard, GuardConfig

A, B = "44.131.1.1", "44.131.1.2"


def ike_v2() -> bytes:
    # Initiator SPI, responder SPI (zero), next payload, version 2.0,
    # IKE_SA_INIT, flags, message id, length.
    body = bytes(range(1, 9)) + bytes(8) + bytes([33, 0x20, 34, 0x08]) + bytes(4)
    return body + struct.pack(">I", 28 + 8) + bytes(8)


QUIC_INITIAL = bytes([0xC3]) + struct.pack(">I", 1) + bytes([8]) + bytes(range(8)) + bytes(1200)
DTLS_HELLO = bytes([22, 0xFE, 0xFD]) + bytes(10) + bytes(60)
OPENVPN_RESET = bytes([7 << 3]) + bytes(range(1, 9)) + b"\0" + b"\0\0\0\0"


@pytest.mark.parametrize("packet, reason", [
    (tcp(A, B, 40000, 8080, TLS_CLIENT_HELLO), "tls"),                 # TLS on a port not on the list
    (tcp(A, B, 40000, 25, bytes.fromhex("1603030050") + bytes(80)), "tls"),  # after STARTTLS
    (tcp(A, B, 40000, 2222, b"SSH-2.0-OpenSSH_9.6\r\n"), "ssh"),
    (ipv4(A, B, 50, bytes(64)), "ipsec-esp"),
    (udp(A, B, 4500, 4500, b"\x12\x34\x56\x78" + bytes(60)), "ipsec-esp"),   # ESP in UDP
    (udp(A, B, 4500, 4500, bytes(4) + ike_v2()), "ike"),
    (udp(A, B, 40000, 5000, ike_v2()), "ike"),                          # IKE on any port
    (udp(A, B, 40000, 5555, b"\x01\0\0\0" + bytes(144)), "wireguard"),
    (udp(A, B, 40000, 5555, b"\x04\0\0\0" + bytes(60)), "wireguard"),
    (udp(A, B, 40000, 1195, OPENVPN_RESET), "openvpn"),
    (tcp(A, B, 40000, 1195, struct.pack(">H", len(OPENVPN_RESET)) + OPENVPN_RESET), "openvpn"),
    (udp(A, B, 40000, 4433, QUIC_INITIAL), "quic"),
    (udp(A, B, 40000, 5684, DTLS_HELLO), "dtls"),
    (tcp(A, B, 40000, 443, b"GET / HTTP/1.1\r\n\r\n"), "denied-port"),  # HTTPS's port, whatever it carries
    (udp(A, B, 40000, 51820, bytes(40)), "denied-port"),
    (ipv4(A, B, 47, struct.pack(">HH", 0, 0x0800) + tcp(A, B, 1, 8080, TLS_CLIENT_HELLO)), "tls"),  # in GRE
    (ipv4(A, B, 4, tcp(A, B, 1, 2222, b"SSH-2.0-x\r\n")), "ssh"),                           # IP in IP
    (bytes([0x60]) + bytes(39), "not-ipv4"),
    (b"\x45\x00\x00", "malformed"),
])
def test_refused(packet, reason):
    guard = EncryptionGuard()
    assert guard.check(packet) == reason
    assert guard.refused[reason] == 1
    assert reason in REFUSALS


@pytest.mark.parametrize("packet", [
    tcp(A, B, 40000, 80, b"GET / HTTP/1.0\r\nHost: example\r\n\r\n"),
    tcp(A, B, 40000, 25, b"EHLO gm4bri.example\r\n"),
    tcp(A, B, 40000, 23, b"login: "),
    udp(A, B, 40000, 123, bytes([0x1B]) + bytes(47)),                   # NTP
    icmp_echo(A, B),
    ipv4(A, B, 51, bytes(24)),                                           # AH authenticates only
    tcp(A, B, 40000, 80, bytes(range(256)) * 2),                         # binary over HTTP
])
def test_ordinary_traffic_passes(packet):
    assert EncryptionGuard().check(packet) is None


def test_no_dns_query_is_mistaken_for_encryption():
    """The single-byte signatures (OpenVPN's opcode, WireGuard's type) must
    not catch DNS, whose first bytes are a random query ID."""
    guard = EncryptionGuard()
    rng = random.Random(1)
    for qid in range(65536):
        if rng.random() > 0.05 and qid >> 8 not in (0x38, 0x40, 0x50, 0x01, 0x02, 0x03, 0x04):
            continue
        query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00\x00\x01\x00\x01"
        assert guard.check(udp(A, B, 40000 + qid % 1000, 53, query)) is None, hex(qid)


def test_the_rest_of_a_refused_connection_is_refused_too():
    guard = EncryptionGuard(clock=lambda: 0.0)
    assert guard.check(tcp(A, B, 40000, 8080, TLS_CLIENT_HELLO)) == "tls"
    # Mid-stream segments look like nothing in particular...
    middle = tcp(A, B, 40000, 8080, bytes(range(200)))
    assert EncryptionGuard().check(middle) is None
    # ...but belong to the flow already refused, in either direction.
    assert guard.check(middle) == "encrypted-flow"
    assert guard.check(tcp(B, A, 8080, 40000, bytes(50))) == "encrypted-flow"
    assert guard.check(tcp(A, B, 40001, 8080, b"GET /")) is None       # another flow


def test_fragments_of_a_refused_datagram_are_refused():
    guard = EncryptionGuard(clock=lambda: 0.0)
    first = tcp(A, B, 40000, 8080, TLS_CLIENT_HELLO[:100], ident=77, more=True)
    assert guard.check(first) == "tls"
    later = ipv4(A, B, 6, bytes(80), ident=77, frag_offset=120)
    assert guard.check(later) == "fragment-of-refused"
    # A later fragment whose first was never seen cannot be judged (documented).
    assert EncryptionGuard().check(ipv4(A, B, 6, bytes(80), ident=78, frag_offset=120)) is None


def test_policy_hooks_and_the_deny_list_are_configurable():
    config = GuardConfig(deny_tcp=frozenset({8080}), extra=[lambda p: "policy" if p.dport == 7000 else None])
    guard = EncryptionGuard(config)
    assert guard.check(tcp(A, B, 1, 8080, b"x")) == "denied-port"
    assert guard.check(tcp(A, B, 1, 443, b"plain")) is None             # no longer on the list
    assert guard.check(udp(A, B, 1, 7000, b"x")) == "policy"
    assert guard.passed == 1 and sum(guard.refused.values()) == 2
