"""A small FLAC decoder, written from the format specification (RFC 9639)
rather than from flac.py, so the tests check the encoder against the format
and not against itself. It decodes what a FLAC file may contain except wasted
bits, escaped partitions and inter-channel decorrelation - none of which the
encoder writes - and checks every CRC and the MD5."""
from __future__ import annotations

import hashlib
import struct

import numpy as np

from shack_rec.flac import crc8, crc16


class Bits:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos * 8

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def s(self, n: int) -> int:
        v = self.u(n)
        return v - (1 << n) if v & (1 << (n - 1)) else v

    def unary(self) -> int:
        q = 0
        while self.u(1) == 0:
            q += 1
        return q

    def align(self) -> None:
        self.pos = (self.pos + 7) // 8 * 8

    @property
    def byte(self) -> int:
        return self.pos >> 3


def _utf8(b: Bits) -> int:
    first = b.u(8)
    if first < 0x80:
        return first
    n = 0
    while first & (0x80 >> n):
        n += 1
    v = first & (0xFF >> (n + 1))
    for _ in range(n - 1):
        v = (v << 6) | (b.u(8) & 0x3F)
    return v


def _residual(b: Bits, n: int, order: int) -> list[int]:
    method = b.u(2)
    width = {0: 4, 1: 5}[method]
    p = b.u(4)
    out = []
    for i in range(1 << p):
        count = (n >> p) - (order if i == 0 else 0)
        k = b.u(width)
        assert k != (1 << width) - 1, "escape code not expected"
        for _ in range(count):
            u = (b.unary() << k) | b.u(k)
            out.append((u >> 1) ^ -(u & 1))
    return out


def _subframe(b: Bits, n: int, bps: int) -> list[int]:
    assert b.u(1) == 0
    kind = b.u(6)
    assert b.u(1) == 0, "wasted bits not expected"
    if kind == 0:
        return [b.s(bps)] * n
    if kind == 1:
        return [b.s(bps) for _ in range(n)]
    if kind & 0b100000:
        order = (kind & 0b11111) + 1
        x = [b.s(bps) for _ in range(order)]
        precision = b.u(4) + 1
        assert precision != 16, "invalid precision"
        shift = b.s(5)
        assert shift >= 0
        q = [b.s(precision) for _ in range(order)]
        for r in _residual(b, n, order):
            x.append(r + (sum(c * x[-1 - i] for i, c in enumerate(q)) >> shift))
        return x
    assert kind & 0b111000 == 0b001000, f"subframe type {kind:06b} not expected"
    order = kind & 7
    x = [b.s(bps) for _ in range(order)]
    coeffs = {0: [], 1: [1], 2: [2, -1], 3: [3, -3, 1], 4: [4, -6, 4, -1]}[order]
    for r in _residual(b, n, order):
        x.append(r + sum(c * x[-1 - i] for i, c in enumerate(coeffs)))
    return x


def decode(data: bytes) -> dict:
    """{"rate", "channels", "bps", "total", "md5", "tags", "samples" (n, ch) int16, "frames"}."""
    assert data[:4] == b"fLaC"
    pos, info, tags = 4, None, {}
    while True:
        last, kind = data[pos] >> 7, data[pos] & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        block = data[pos + 4:pos + 4 + length]
        if kind == 0:
            minb, maxb = struct.unpack(">HH", block[:4])
            packed = int.from_bytes(block[10:18], "big")
            info = {"min_block": minb, "max_block": maxb,
                    "min_frame": int.from_bytes(block[4:7], "big"), "max_frame": int.from_bytes(block[7:10], "big"),
                    "rate": packed >> 44, "channels": ((packed >> 41) & 7) + 1, "bps": ((packed >> 36) & 31) + 1,
                    "total": packed & ((1 << 36) - 1), "md5": block[18:34]}
        elif kind == 4:
            vlen = struct.unpack("<I", block[:4])[0]
            off = 4 + vlen
            count = struct.unpack("<I", block[off:off + 4])[0]
            off += 4
            for _ in range(count):
                n = struct.unpack("<I", block[off:off + 4])[0]
                k, _sep, v = block[off + 4:off + 4 + n].decode().partition("=")
                tags[k] = v
                off += 4 + n
        pos += 4 + length
        if last:
            break
    chans, frames, sizes = [[] for _ in range(info["channels"])], 0, []
    while pos < len(data):
        start = pos
        b = Bits(data, pos)
        assert b.u(15) == 0x7FFC, "lost frame sync"
        assert b.u(1) == 0, "variable block size not expected"
        bs, rc = b.u(4), b.u(4)
        ch, ss = b.u(4), b.u(3)
        assert b.u(1) == 0
        assert ss == 0b100 and info["bps"] == 16
        assert ch == info["channels"] - 1, "independent channels expected"
        assert _utf8(b) == frames
        if bs == 6:
            n = b.u(8) + 1
        elif bs == 7:
            n = b.u(16) + 1
        elif 8 <= bs <= 15:
            n = 256 << (bs - 8)
        else:
            n = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608}[bs]
        if rc == 12:
            rate = b.u(8) * 1000
        elif rc == 13:
            rate = b.u(16)
        elif rc == 14:
            rate = b.u(16) * 10
        else:
            rate = None
        assert crc8(data[start:b.byte]) == b.u(8), "frame header CRC-8"
        if rate is not None:
            assert rate == info["rate"]
        for c in range(info["channels"]):
            chans[c] += _subframe(b, n, 16)
        b.align()
        crc = crc16(data[start:b.byte])
        assert crc == b.u(16), "frame CRC-16"
        sizes.append(b.byte - start)
        pos = b.byte
        frames += 1
    samples = np.array(chans, np.int64).T
    assert samples.min(initial=0) >= -32768 and samples.max(initial=0) <= 32767
    samples = samples.astype(np.int16)
    if info["total"]:
        assert info["total"] == len(samples)
    if any(info["md5"]):
        assert hashlib.md5(samples.astype("<i2").tobytes()).digest() == info["md5"], "MD5"
    if sizes:
        assert info["min_frame"] in (0, min(sizes)) and info["max_frame"] in (0, max(sizes))
    return {**info, "tags": tags, "samples": samples, "frames": frames}
