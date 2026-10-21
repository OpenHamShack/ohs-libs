"""IP over AX.25 between engines: ping and UDP both ways in datagram and
virtual-circuit mode, ARP (direct and through a digipeater), routes, and
the encryption guard refusing before anything is keyed."""

from __future__ import annotations

import ipaddress

import pytest
from ippackets import TLS_CLIENT_HELLO, icmp_echo, parse, tcp, udp
from stations import World

from shack_packet.digi import DigiConfig, Digipeater
from shack_packet.ip import IpConfig, IpInterface

A_IP, B_IP = "44.131.1.1", "44.131.1.2"


def pair(mode="datagram", arp=True, **world_kw):
    world = World(**world_kw)
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    ia = IpInterface(a, IpConfig(A_IP, "44.131.0.0/16", mode=mode))
    ib = IpInterface(b, IpConfig(B_IP, "44.131.0.0/16", mode=mode, answer_ping=True))
    if arp:
        ia.add_arp(B_IP, "GM4BRI", mode=mode)
        ib.add_arp(A_IP, "G0ABC", mode=mode)
    got_a, got_b = [], []
    ia.on_packet, ib.on_packet = got_a.append, got_b.append
    return world, a, b, ia, ib, got_a, got_b


def udp_echo(interface, got):
    """B's side of a UDP echo, as a host stack's echo service would."""
    def on_packet(packet):
        got.append(packet)
        p = parse(packet)
        if p["proto"] == 17 and p["dport"] == 7:
            interface.send(udp(p["dst"], p["src"], 7, p["sport"], p["data"]))
    interface.on_packet = on_packet


@pytest.mark.parametrize("mode", ["datagram", "vc"])
def test_ping_and_udp_round_trip(mode):
    world, a, b, ia, ib, got_a, got_b = pair(mode, loss=0.05, seed=3)
    assert ia.send(icmp_echo(A_IP, B_IP, seq=1))
    assert world.run(120, lambda: got_a)
    reply = parse(got_a[0])
    assert (reply["src"], reply["dst"], reply["type"]) == (B_IP, A_IP, 0)
    assert reply["valid_header"] and reply["icmp_valid"] and reply["data"] == b"ping from the tests"
    udp_echo(ib, got_b)
    got_a.clear()
    payload = b"hello over AX.25 " * (30 if mode == "vc" else 5)   # 510 bytes: above N1 in vc mode
    assert ia.send(udp(A_IP, B_IP, 40000, 7, payload))
    assert world.run(300, lambda: got_a)
    echoed = parse(got_a[0])
    assert (echoed["sport"], echoed["dport"], echoed["data"]) == (7, 40000, payload)
    if mode == "vc":
        assert ia.stats["sent_circuit"] >= 1 and ib.stats["received_circuit"] >= 2
        assert ia._circuits["GM4BRI"].connected
    else:
        assert ia.stats["sent_datagram"] == 2 and not a.connections


def test_datagram_mode_refuses_more_than_the_mtu():
    world, a, b, ia, ib, got_a, got_b = pair()
    assert not ia.send(udp(A_IP, B_IP, 1, 2, b"x" * 400))
    assert a.events[-1].kind == "ip-refused" and a.events[-1].reason == "too-big"


def test_arp_finds_the_station_and_the_packet_waits_for_it():
    world, a, b, ia, ib, got_a, got_b = pair(arp=False)
    assert ia.send(icmp_echo(A_IP, B_IP))
    assert world.run(120, lambda: got_a)
    assert str(ia.arp[ipaddress.IPv4Address(B_IP)].call) == "GM4BRI"
    assert ia.stats["arp_requests"] == 1 and ib.stats["arp_replies"] == 1
    # B learned A from the request, so its reply needed no ARP of its own.
    assert ib.stats["arp_requests"] == 0


def test_arp_through_a_digipeater_learns_the_path():
    world = World()
    a = world.station("G0ABC")
    d = world.station("MB7UC")
    b = world.station("GM4BRI")
    world.medium.hear("G0ABC", "GM4BRI", False)
    Digipeater(d, DigiConfig(enabled=True))
    ia = IpInterface(a, IpConfig(A_IP, "44.131.0.0/16", via=["MB7UC"]))
    ib = IpInterface(b, IpConfig(B_IP, "44.131.0.0/16", answer_ping=True))
    got = []
    ia.on_packet = got.append
    assert ia.send(icmp_echo(A_IP, B_IP))
    assert world.run(200, lambda: got)
    entry = ia.arp[ipaddress.IPv4Address(B_IP)]
    assert [str(h) for h in entry.via] == ["MB7UC"]


def test_arp_gives_up_and_says_so():
    world = World()
    a = world.station("G0ABC")
    ia = IpInterface(a, IpConfig(A_IP, "44.131.0.0/16"))
    assert ia.send(icmp_echo(A_IP, B_IP))
    assert world.run(120, lambda: any(e.reason == "arp-failed" for e in a.events))
    assert ia.stats["arp_requests"] == 3


def test_routes_pick_the_gateway_and_unknown_networks_are_refused():
    world, a, b, ia, ib, got_a, got_b = pair()
    ia.add_route("0.0.0.0/0", gateway=B_IP)
    assert ia.send(icmp_echo(A_IP, "192.0.2.9"))
    assert world.run(60, lambda: got_b)
    assert parse(got_b[0])["dst"] == "192.0.2.9"                # handed to the gateway
    world2 = World()
    lone = IpInterface(world2.station("G0ABC"), IpConfig(A_IP, "44.131.0.0/16"))
    assert not lone.send(icmp_echo(A_IP, "192.0.2.9"))
    assert lone.stats["refused_no-route"] == 1


def test_the_guard_refuses_before_anything_is_keyed():
    world, a, b, ia, ib, got_a, got_b = pair()
    refusals = []
    a.on_event = lambda e: refusals.append(e.reason) if e.kind == "ip-refused" else None
    assert not ia.send(tcp(A_IP, B_IP, 40001, 8080, TLS_CLIENT_HELLO))
    assert not ia.send(tcp(A_IP, B_IP, 40002, 2222, b"SSH-2.0-OpenSSH_9.6\r\n"))
    assert not ia.send(udp(A_IP, B_IP, 40003, 5353, b"\x01\0\0\0" + bytes(range(144))))  # a WireGuard handshake
    world.run(30)
    assert refusals == ["tls", "ssh", "wireguard"]
    assert a.channel.stats.transmissions == 0 and not got_b
    assert ia.guard.refused["tls"] == 1 and ia.guard.refused["ssh"] == 1
    # Plain traffic still goes.
    assert ia.send(tcp(A_IP, B_IP, 40004, 80, b"GET / HTTP/1.0\r\n\r\n"))
    assert world.run(30, lambda: got_b)
