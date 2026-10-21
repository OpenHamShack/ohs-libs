"""IPv4 packets built by hand for the IP and guard tests (as helpers.py
builds frames: not with the code under test)."""

from __future__ import annotations

import ipaddress
import struct


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ipv4(src, dst, proto, payload, ident=1, frag_offset=0, more=False, ttl=64) -> bytes:
    flags = (0x2000 if more else 0) | (frag_offset // 8)
    header = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, flags, ttl, proto, 0,
                         ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed)
    header = header[:10] + struct.pack(">H", checksum(header)) + header[12:]
    return header + payload


def icmp_echo(src, dst, ident=7, seq=1, data=b"ping from the tests", reply=False) -> bytes:
    body = struct.pack(">BBHHH", 0 if reply else 8, 0, 0, ident, seq) + data
    body = body[:2] + struct.pack(">H", checksum(body)) + body[4:]
    return ipv4(src, dst, 1, body)


def udp(src, dst, sport, dport, data: bytes, **kw) -> bytes:
    return ipv4(src, dst, 17, struct.pack(">HHHH", sport, dport, 8 + len(data), 0) + data, **kw)


def tcp(src, dst, sport, dport, data: bytes = b"", flags=0x18, **kw) -> bytes:
    header = struct.pack(">HHIIBBHHH", sport, dport, 1000, 0, 5 << 4, flags, 65535, 0, 0)
    return ipv4(src, dst, 6, header + data, **kw)


def parse(packet: bytes) -> dict:
    ihl = (packet[0] & 0x0F) * 4
    out = {"src": str(ipaddress.IPv4Address(packet[12:16])), "dst": str(ipaddress.IPv4Address(packet[16:20])),
           "proto": packet[9], "valid_header": checksum(packet[:ihl]) == 0}
    body = packet[ihl:]
    if out["proto"] == 1:
        out.update(type=body[0], icmp_valid=checksum(body) == 0, data=body[8:])
    elif out["proto"] == 17:
        out.update(sport=struct.unpack(">H", body[:2])[0], dport=struct.unpack(">H", body[2:4])[0], data=body[8:])
    return out


TLS_CLIENT_HELLO = bytes.fromhex("16030100c8010000c40303") + bytes(32) + b"\x00" + bytes.fromhex("0002c02f0100") \
    + bytes(160)
