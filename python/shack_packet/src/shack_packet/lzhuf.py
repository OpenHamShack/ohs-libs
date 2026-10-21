"""LZHUF, as Winlink's B2 forwarding uses it: LZSS with a 2 KiB window
and adaptive Huffman codes, with the uncompressed length in front and,
in B2, a CRC-16 of everything after it in front of that.

    blob = compress(b"text", crc=True)        # what a B2 proposal carries
    decompress(blob, crc=True) == b"text"

**Where it comes from.** A line-for-line Python port of the Go package
`lzhuf` in wl2k-go (github.com/la5nta/wl2k-go, MIT licence, Copyright
2016 Martin Hebnes Pedersen LA5NTA), which is itself a port of lzhuf.c
from JNOS 2 (LZSS by Haruhiko Okumura, adaptive Huffman by Haruyasu
Yoshizaki, edited by Kenji Rikitake; its authors' terms are "use,
distribute, and modify this program freely" - wl2k-go's lzhuf/COPYRIGHT
has the history). Both are compatible with this package's GPL-3.0; the
notice is in packaging/THIRD-PARTY of the apps that ship it.

**Why a port and not a rewrite from the paper:** every Winlink gateway,
Winlink Express and Pat agree on this exact bit stream, quirks included
(the text buffer starts as spaces; ties in the match search go to the
nearest position; the B2 CRC is CRC-16/XMODEM fed two zero bytes at the
end). A cleaner LZH would decode the same data and still fail to make
bytes a gateway accepts, so the tests check our output against
wl2k-go's own test vectors byte for byte.

Speed: pure Python, about 100-200 KB/s on shack - far faster than any
packet link a message will cross. Sizes are capped by the callers
(b2f.py), not here.
"""
from __future__ import annotations

import struct

_N = 2048                       # ring buffer size
_F = 60                         # look-ahead size
_THRESHOLD = 2
_NIL = _N
_N_CHAR = 256 - _THRESHOLD + _F
_T = _N_CHAR * 2 - 1
_R = _T - 1
_MAX_FREQ = 0x8000


class ChecksumError(ValueError):
    """The CRC or the length in the header does not match the data."""


# The upper six bits of a match position: codes and lengths for encoding,
# and the reverse tables for decoding (lzhuf.c's p_code/p_len/d_code/d_len).
_P_CODE = bytes([
    0x00, 0x20, 0x30, 0x40, 0x50, 0x58, 0x60, 0x68, 0x70, 0x78, 0x80, 0x88, 0x90, 0x94, 0x98, 0x9C,
    0xA0, 0xA4, 0xA8, 0xAC, 0xB0, 0xB4, 0xB8, 0xBC, 0xC0, 0xC2, 0xC4, 0xC6, 0xC8, 0xCA, 0xCC, 0xCE,
    0xD0, 0xD2, 0xD4, 0xD6, 0xD8, 0xDA, 0xDC, 0xDE, 0xE0, 0xE2, 0xE4, 0xE6, 0xE8, 0xEA, 0xEC, 0xEE,
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF])
_P_LEN = bytes([3] + [4] * 3 + [5] * 8 + [6] * 12 + [7] * 24 + [8] * 16)


def _decode_tables() -> tuple[bytes, bytes]:
    """d_code/d_len, built from p_code/p_len: every byte whose top bits
    are a position code maps to that code's index and length."""
    code, length = bytearray(256), bytearray(256)
    for i in range(64):
        n = _P_LEN[i]
        start = _P_CODE[i]
        for b in range(start, start + (1 << (8 - n))):
            code[b] = i
            length[b] = n
    return bytes(code), bytes(length)


_D_CODE, _D_LEN = _decode_tables()

_CRC_TAB = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if _c & 0x8000 else (_c << 1)
    _CRC_TAB.append(_c & 0xFFFF)


def crc16(data: bytes) -> int:
    """B2's CRC: the XMODEM table, updated LZHUF's way (each byte
    shifted in at the bottom), then two zero bytes fed through."""
    s = 0
    for c in bytes(data) + b"\0\0":
        s = (((s << 8) & 0xFF00) ^ _CRC_TAB[(s >> 8) & 0xFF] ^ c) & 0xFFFF
    return s


class _Huffman:
    """The adaptive Huffman tree both directions share."""

    def __init__(self):
        self.freq = [0] * (_T + 1)
        self.prnt = [0] * (_T + _N_CHAR)
        self.son = [0] * _T
        for i in range(_N_CHAR):
            self.freq[i] = 1
            self.son[i] = i + _T
            self.prnt[i + _T] = i
        i, j = 0, _N_CHAR
        while j <= _R:
            self.freq[j] = self.freq[i] + self.freq[i + 1]
            self.son[j] = i
            self.prnt[i] = self.prnt[i + 1] = j
            i += 2
            j += 1
        self.freq[_T] = 0xFFFF
        self.prnt[_R] = 0

    def reconst(self) -> None:
        freq, son, prnt = self.freq, self.son, self.prnt
        j = 0
        for i in range(_T):
            if son[i] >= _T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, _N_CHAR
        while j < _T:
            f = freq[i] + freq[i + 1]
            freq[j] = f
            k = j
            while f < freq[k - 1]:
                k -= 1
            n = j - k
            freq[k + 1:k + 1 + n] = freq[k:k + n]
            freq[k] = f
            son[k + 1:k + 1 + n] = son[k:k + n]
            son[k] = i
            i += 2
            j += 1
        for i in range(_T):
            k = son[i]
            if k >= _T:
                prnt[k] = i
            else:
                prnt[k] = prnt[k + 1] = i

    def update(self, c: int) -> None:
        freq, son, prnt = self.freq, self.son, self.prnt
        if freq[_R] == _MAX_FREQ:
            self.reconst()
        c = prnt[c + _T]
        while True:
            freq[c] += 1
            k = freq[c]
            if c + 2 >= len(freq) or k <= freq[c + 1]:
                c = prnt[c]
                if c == 0:
                    break
                continue
            ll = c + 1
            while k > freq[ll + 1]:
                ll += 1
            freq[c] = freq[ll]
            freq[ll] = k
            i = son[c]
            prnt[i] = ll
            if i < _T:
                prnt[i + 1] = ll
            j = son[ll]
            son[ll] = i
            prnt[j] = c
            if j < _T:
                prnt[j + 1] = c
            son[c] = j
            c = prnt[ll]
            if c == 0:
                break


class _Tree:
    """LZSS's binary search trees over the ring buffer (encoder only)."""

    def __init__(self, text: bytearray):
        self.text = text
        self.dad = [_NIL] * (_N + 1)
        self.lson = [0] * (_N + 1)
        self.rson = [0] * (_N + 257)
        for i in range(_N + 1, _N + 257):
            self.rson[i] = _NIL
        self.match_length = 0
        self.match_position = 0

    def insert(self, r: int) -> None:
        text, dad, lson, rson = self.text, self.dad, self.lson, self.rson
        cmp = 1
        p = _N + 1 + text[r]
        rson[r] = lson[r] = _NIL
        self.match_length = 0
        while True:
            if cmp >= 0:
                if rson[p] != _NIL:
                    p = rson[p]
                else:
                    rson[p] = r
                    dad[r] = p
                    return
            else:
                if lson[p] != _NIL:
                    p = lson[p]
                else:
                    lson[p] = r
                    dad[r] = p
                    return
            i = 1
            while i < _F:
                cmp = text[r + i] - text[p + i]
                if cmp != 0:
                    break
                i += 1
            if i > _THRESHOLD:
                if i > self.match_length:
                    self.match_position = ((r - p) & (_N - 1)) - 1
                    self.match_length = i
                    if i >= _F:
                        break
                if i == self.match_length:
                    c = ((r - p) & (_N - 1)) - 1
                    if c < self.match_position:
                        self.match_position = c
        dad[r] = dad[p]
        lson[r] = lson[p]
        rson[r] = rson[p]
        dad[lson[p]] = r
        dad[rson[p]] = r
        if rson[dad[p]] == p:
            rson[dad[p]] = r
        else:
            lson[dad[p]] = r
        dad[p] = _NIL

    def delete(self, p: int) -> None:
        dad, lson, rson = self.dad, self.lson, self.rson
        if dad[p] == _NIL:
            return
        if rson[p] == _NIL:
            q = lson[p]
        elif lson[p] == _NIL:
            q = rson[p]
        else:
            q = lson[p]
            if rson[q] != _NIL:
                while rson[q] != _NIL:
                    q = rson[q]
                rson[dad[q]] = lson[q]
                dad[lson[q]] = dad[q]
                lson[q] = lson[p]
                dad[lson[p]] = q
            rson[q] = rson[p]
            dad[rson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p:
            rson[dad[p]] = q
        else:
            lson[dad[p]] = q
        dad[p] = _NIL


class _Encoder:
    def __init__(self):
        self.huff = _Huffman()
        self.text = bytearray(b" " * (_N - _F) + b"\0" * (_N + _F - 1 - (_N - _F)))
        self.tree = _Tree(self.text)
        self.out = bytearray()
        self.putbuf = 0
        self.putlen = 0

    def put_code(self, n: int, c: int) -> None:
        self.putbuf = (self.putbuf | (c >> self.putlen)) & 0xFFFF
        self.putlen += n
        if self.putlen < 8:
            return
        self.out.append(self.putbuf >> 8)
        self.putlen -= 8
        if self.putlen >= 8:
            self.out.append(self.putbuf & 0xFF)
            self.putlen -= 8
            self.putbuf = (c << (n - self.putlen)) & 0xFFFF
        else:
            self.putbuf = (self.putbuf << 8) & 0xFFFF

    def encode_char(self, c: int) -> None:
        prnt = self.huff.prnt
        i = j = 0
        k = prnt[c + _T]
        while True:
            i >>= 1
            j += 1
            if k & 1:
                i += 0x8000
            k = prnt[k]
            if k == _R:
                break
        self.put_code(j, i)
        self.huff.update(c)

    def encode_position(self, c: int) -> None:
        i = c >> 6
        self.put_code(_P_LEN[i], _P_CODE[i] << 8)
        self.put_code(6, (c & 0x3F) << 10)

    def run(self, data: bytes) -> bytes:
        text, tree = self.text, self.tree
        r, s, length = _N - _F, 0, 0
        n = 0
        size = len(data)
        # Fill the look-ahead.
        while length < _F and n < size:
            text[r + length] = data[n]
            n += 1
            length += 1
        if length == 0:
            return b""
        for i in range(1, _F + 1):
            tree.insert(r - i)
        tree.insert(r)
        while True:
            if tree.match_length > length:
                tree.match_length = length
            if tree.match_length <= _THRESHOLD:
                tree.match_length = 1
                self.encode_char(text[r])
            else:
                self.encode_char(255 - _THRESHOLD + tree.match_length)
                self.encode_position(tree.match_position)
            last = tree.match_length
            i = 0
            while i < last and n < size:
                tree.delete(s)
                c = data[n]
                n += 1
                text[s] = c
                if s < _F - 1:
                    text[s + _N] = c
                s = (s + 1) & (_N - 1)
                r = (r + 1) & (_N - 1)
                tree.insert(r)
                i += 1
            while i < last:
                i += 1
                tree.delete(s)
                s = (s + 1) & (_N - 1)
                r = (r + 1) & (_N - 1)
                length -= 1
                if length:
                    tree.insert(r)
            if length <= 0:
                break
        if self.putlen:
            self.out.append(self.putbuf >> 8)
        return bytes(self.out)


def compress(data: bytes, crc: bool = True) -> bytes:
    """`data` compressed; with `crc` (B2), the CRC-16 of the length and
    the compressed bytes first, little-endian, as Winlink sends it."""
    body = struct.pack("<i", len(data)) + _Encoder().run(bytes(data))
    if crc:
        return struct.pack("<H", crc16(body)) + body
    return body


class _Bits:
    def __init__(self, data: bytes, start: int):
        self.data = data
        self.at = start
        self.n = 0
        self.bits = 0

    def read(self, count: int) -> int:
        while count > self.bits:
            if self.at >= len(self.data):
                raise ChecksumError("the compressed data ends early")
            self.n = ((self.n << 8) | self.data[self.at]) & 0xFFFFFFFF
            self.at += 1
            self.bits += 8
        self.bits -= count
        return (self.n >> self.bits) & ((1 << count) - 1)


def decompress(blob: bytes, crc: bool = True, *, max_size: int = 16 * 1024 * 1024) -> bytes:
    """The data back; ChecksumError when the CRC (B2) or the length does
    not match, or the stream ends early. `max_size` refuses a header that
    claims more than any message we would accept, before decoding it."""
    blob = bytes(blob)
    at = 0
    if crc:
        if len(blob) < 6:
            raise ChecksumError("too short for a B2 header")
        (want,) = struct.unpack_from("<H", blob, 0)
        if crc16(blob[2:]) != want:
            raise ChecksumError("CRC mismatch")
        at = 2
    if len(blob) < at + 4:
        raise ChecksumError("too short for a length")
    (size,) = struct.unpack_from("<i", blob, at)
    if size < 0 or size > max_size:
        raise ChecksumError("a length out of range")
    bits = _Bits(blob, at + 4)
    huff = _Huffman()
    son = huff.son
    text = bytearray(b" " * (_N - _F) + b"\0" * _F)
    r = _N - _F
    out = bytearray()
    while len(out) < size:
        c = son[_R]
        while c < _T:
            c = son[c + bits.read(1)]
        c -= _T
        huff.update(c)
        if c < 256:
            out.append(c)
            text[r] = c
            r = (r + 1) & (_N - 1)
            continue
        i = bits.read(8)
        hi = _D_CODE[i] << 6
        j = _D_LEN[i] - 2
        while j > 0:
            i = (i << 1) + bits.read(1)
            j -= 1
        pos = hi | (i & 0x3F)
        i = (r - pos - 1) & (_N - 1)
        for k in range(c - 255 + _THRESHOLD):
            ch = text[(i + k) & (_N - 1)]
            out.append(ch)
            text[r] = ch
            r = (r + 1) & (_N - 1)
    if len(out) != size:
        raise ChecksumError("length mismatch")
    return bytes(out)
