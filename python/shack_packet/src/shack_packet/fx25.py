"""FX.25: Reed-Solomon forward error correction wrapped round an ordinary
AX.25 frame, so that a station that has never heard of it still decodes it.

The FX.25 draft (Stensat Group, 0.01, September 2006) and direwolf's
implementation, which is what FX.25 means on the air today, agree on this
layout. A transmission is

    preamble flags | correlation tag (8 bytes) | data block | check bytes | tail flags

all under one NRZI, none of it bit-stuffed. The **data block** is the frame
exactly as plain AX.25 would send it - a flag, the bit-stuffed frame and FCS,
a flag - padded to the block's size with more flags (continued bit by bit, so
the padding is a run of real flags in the bit stream, not byte-aligned ones).
That is the whole of the compatibility trick: a plain receiver sees eight
bytes of noise, then a perfectly good frame between flags, then some more
noise, and decodes the frame. An FX.25 receiver spots the tag, takes the
block and the check bytes whole, corrects them, and takes the same frame out.

**The correlation tag** says which code follows and marks where the block
starts. Eleven are defined; they are 64-bit words any two of which differ in
exactly 32 bits, so one can be recognised with errors in it. Up to 8 wrong
bits are accepted, as direwolf does: its author ran it for two months as a
digipeater and iGate with no false trigger, and the arithmetic agrees - a
random 64-bit word lands within 8 bits of one of 11 tags about once in
4 x 10^8 tries, which at 1200 bit/s is once in a few days per slicer, and a
false trigger costs one failed RS decode, never a wrong frame.

**The codes** are RS(255,239), RS(255,223) and RS(255,191) over GF(2^8) with
field polynomial 0x11D, first consecutive root 1 and primitive element 1 (16,
32 and 64 check bytes; corrects 8, 16 and 32 wrong bytes), each also
shortened: the block occupies the first k bytes of the 255, the rest of the
data area is zeros that are never sent, and the check bytes sit at the end.
The decoder below is our own (Berlekamp-Massey, Chien search, Forney), checked
against direwolf in both directions (tests/test_direwolf.py); the generator
and byte order are Phil Karn's, which direwolf uses and which every FX.25
station therefore uses too.

**A correction is never trusted on its own.** RS can "correct" a block with
too many errors into a different valid codeword. Two checks catch that: an
error located in the shortened (never sent) part of the block is impossible,
so the block is refused; and the corrected frame must still pass its own
AX.25 FCS.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import hdlc
from .errors import PacketError

# -- GF(2^8) -----------------------------------------------------------------

_POLY = 0x11D
_FCR = 1
_PRIM = 1
NN = 255

EXP = np.zeros(512, dtype=np.int64)
LOG = np.zeros(256, dtype=np.int64)
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= _POLY
EXP[255:510] = EXP[0:255]
LOG[0] = -1                                    # log of zero: never used as one
_EXP = [int(v) for v in EXP]
_LOG = [int(v) for v in LOG]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def _poly_eval(poly: list[int], x: int) -> int:
    """poly[0] is the constant term."""
    out = 0
    for coef in reversed(poly):
        out = _mul(out, x) ^ coef
    return out


class ReedSolomon:
    """RS(255, 255-nroots) as FX.25 uses it. `encode` takes the 255-nroots
    data bytes (zeros where the code is shortened) and returns the check
    bytes; `decode` takes all 255 and returns them corrected."""

    def __init__(self, nroots: int):
        self.nroots = nroots
        self.k = NN - nroots
        # g(x) = prod (x - a^(fcr+i)*prim), highest power first.
        g = [1]
        for i in range(nroots):
            root = _EXP[((_FCR + i) * _PRIM) % 255]
            nxt = g + [0]
            for j in range(1, len(nxt)):
                nxt[j] ^= _mul(g[j - 1], root)
            g = nxt
        self.genpoly = g                       # len nroots+1, g[0] == 1
        # Syndrome matrix: S_j = sum_i r_i a^((fcr+j)(254-i)), as logs.
        j = np.arange(nroots)[:, None]
        i = np.arange(NN)[None, :]
        self._syn_log = ((_FCR + j) * _PRIM * (NN - 1 - i)) % 255

    def encode(self, data: bytes) -> bytes:
        if len(data) != self.k:
            raise PacketError(f"RS({NN},{self.k}) encodes {self.k} bytes, not {len(data)}")
        g = self.genpoly[1:]
        parity = [0] * self.nroots
        for byte in data:
            feedback = byte ^ parity[0]
            parity = parity[1:] + [0]
            if feedback:
                lf = _LOG[feedback]
                for j, gj in enumerate(g):
                    if gj:
                        parity[j] ^= _EXP[lf + _LOG[gj]]
        return bytes(parity)

    def syndromes(self, block) -> np.ndarray:
        r = np.frombuffer(bytes(block), dtype=np.uint8).astype(np.int64)
        nz = r != 0
        if not nz.any():
            return np.zeros(self.nroots, dtype=np.int64)
        terms = EXP[(self._syn_log[:, nz] + LOG[r[nz]][None, :]) % 255]
        return np.bitwise_xor.reduce(terms, axis=1)

    def decode(self, block: bytes, forbidden=()) -> tuple[bytes, list[int]] | None:
        """The 255-byte block corrected, and the positions corrected; None
        when there are more errors than the code can fix, or when the fix
        lands in a position in `forbidden` (the shortened zeros, which were
        never sent and so cannot be wrong)."""
        if len(block) != NN:
            raise PacketError(f"a block is {NN} bytes, not {len(block)}")
        synd = [int(s) for s in self.syndromes(block)]
        if not any(synd):
            return bytes(block), []

        # Berlekamp-Massey: the error locator Lambda(x), constant term first.
        lam = [1] + [0] * self.nroots
        prev = [1] + [0] * self.nroots
        length, m, b = 0, 1, 1
        for n in range(self.nroots):
            d = synd[n]
            for i in range(1, length + 1):
                d ^= _mul(lam[i], synd[n - i])
            if d == 0:
                m += 1
                continue
            coef = _div(d, b)
            if 2 * length <= n:
                saved = lam[:]
                for i in range(m, self.nroots + 1):
                    lam[i] ^= _mul(coef, prev[i - m])
                length, prev, b, m = n + 1 - length, saved, d, 1
            else:
                for i in range(m, self.nroots + 1):
                    lam[i] ^= _mul(coef, prev[i - m])
                m += 1
        lam = lam[:length + 1]
        if length == 0 or 2 * length > self.nroots:
            return None

        # Chien search: block position i is x^(254-i); it is in error when
        # Lambda(a^-(254-i)) is zero.
        positions = []
        for i in range(NN):
            if _poly_eval(lam, _EXP[(255 - (NN - 1 - i)) % 255]) == 0:
                positions.append(i)
        if len(positions) != length:
            return None
        bad = set(forbidden)
        if any(p in bad for p in positions):
            return None

        # Forney: Omega(x) = S(x) Lambda(x) mod x^nroots, and the magnitude at
        # X = a^(254-i) is X^(1-fcr) Omega(X^-1) / Lambda'(X^-1).
        omega = [0] * self.nroots
        for i, s in enumerate(synd):
            if s:
                for j, lj in enumerate(lam):
                    if i + j < self.nroots and lj:
                        omega[i + j] ^= _mul(s, lj)
        dlam = [lam[i] if i % 2 == 1 else 0 for i in range(1, len(lam))]
        out = bytearray(block)
        for p in positions:
            power = NN - 1 - p
            x_inv = _EXP[(255 - power) % 255]
            denom = _poly_eval(dlam, x_inv)
            if denom == 0:
                return None
            value = _div(_poly_eval(omega, x_inv), denom)
            value = _mul(value, _EXP[(power * (1 - _FCR)) % 255])
            out[p] ^= value
        # Belt and braces: a block the locator mis-described still has
        # syndromes, and is not a codeword whatever the arithmetic said.
        if self.syndromes(out).any():
            return None
        return bytes(out), positions


_CODECS: dict[int, ReedSolomon] = {}


def codec(nroots: int) -> ReedSolomon:
    if nroots not in _CODECS:
        _CODECS[nroots] = ReedSolomon(nroots)
    return _CODECS[nroots]


# -- the tags ----------------------------------------------------------------


@dataclass(frozen=True)
class Tag:
    number: int
    value: int            # 64 bits, sent least significant first
    block: int            # bytes on the air after the tag: data + check
    data: int             # data bytes on the air
    nroots: int

    @property
    def bits(self) -> list[int]:
        return [(self.value >> i) & 1 for i in range(64)]


#: The eleven defined tags (FX.25 0.01 table 1; direwolf fx25_init.c).
TAGS = {t.number: t for t in (
    Tag(0x01, 0xB74DB7DF8A532F3E, 255, 239, 16),
    Tag(0x02, 0x26FF60A600CC8FDE, 144, 128, 16),
    Tag(0x03, 0xC7DC0508F3D9B09E, 80, 64, 16),
    Tag(0x04, 0x8F056EB4369660EE, 48, 32, 16),
    Tag(0x05, 0x6E260B1AC5835FAE, 255, 223, 32),
    Tag(0x06, 0xFF94DC634F1CFF4E, 160, 128, 32),
    Tag(0x07, 0x1EB7B9CDBC09C00E, 96, 64, 32),
    Tag(0x08, 0xDBF869BD2DBB1776, 64, 32, 32),
    Tag(0x09, 0x3ADB0C13DEAE2836, 255, 191, 64),
    Tag(0x0A, 0xAB69DB6A543188D6, 192, 128, 64),
    Tag(0x0B, 0x4A4ABEC4A724B796, 128, 64, 64),
)}

#: How many of a tag's 64 bits may be wrong and still be recognised.
TAG_TOLERANCE = 8

#: The automatic choice, smallest block first, and why this order: short
#: frames get 16 check bytes (they are short, so few errors land in them),
#: middling ones 32 or 64, and the longest only fit with fewer check bytes.
#: It is direwolf's order (which follows UZ7HO's soundmodem), kept so a
#: station here picks the same code a direwolf beside it would.
_AUTO = (0x04, 0x03, 0x06, 0x09, 0x05, 0x01)


def pick_tag(data_len: int, check: int | None = None) -> Tag | None:
    """The tag for `data_len` bytes of data block. `check` asks for 16, 32 or
    64 check bytes (the smallest block that fits); None chooses as direwolf
    does. None back means the frame is too long for FX.25 and goes as plain
    AX.25."""
    if check is None:
        for number in _AUTO:
            if data_len <= TAGS[number].data:
                return TAGS[number]
        return None
    if check not in (16, 32, 64):
        raise PacketError(f"FX.25 has 16, 32 or 64 check bytes, not {check}")
    fits = [t for t in TAGS.values() if t.nroots == check and data_len <= t.data]
    return min(fits, key=lambda t: t.data) if fits else None


# -- transmit ----------------------------------------------------------------


def data_block_bits(frame: bytes) -> list[int]:
    """The frame as plain AX.25 sends it - a flag, the stuffed frame and its
    FCS, a flag - before the padding."""
    return list(hdlc.FLAG_BITS) + hdlc.stuff(hdlc.with_fcs(frame)) + list(hdlc.FLAG_BITS)


def frame_bits(frame: bytes, check: int | None = None, tag: int | None = None) -> list[int] | None:
    """One frame (without its FCS) as FX.25 data bits: tag, data block,
    check bytes. Data bits, before NRZI. None if the frame is too long for
    any FX.25 block (or the one asked for), and the caller sends plain AX.25:
    the longest block carries 239 bytes, so a frame of more than about 230
    bytes after stuffing has no FX.25 form."""
    bits = data_block_bits(frame)
    need = (len(bits) + 7) // 8
    if tag is not None:
        chosen = TAGS[tag] if tag in TAGS and need <= TAGS[tag].data else None
    else:
        chosen = pick_tag(need, check)
    if chosen is None:
        return None
    # The padding continues the flag pattern bit by bit from wherever the
    # closing flag ended, so the stream stays a run of flags.
    at = 0
    while len(bits) < chosen.data * 8:
        bits.append(hdlc.FLAG_BITS[at % 8])
        at += 1
    data = bytes(sum(bits[i * 8 + k] << k for k in range(8)) for i in range(chosen.data))
    rs = codec(chosen.nroots)
    check_bytes = rs.encode(data + bytes(rs.k - chosen.data))
    return chosen.bits + hdlc.byte_bits(data) + hdlc.byte_bits(check_bytes)


# -- receive -----------------------------------------------------------------


def unstuff_block(data: bytes) -> bytes | None:
    """The frame (with FCS) out of a corrected data block, or None. The block
    must start with a flag; more may follow; the frame runs to the next flag,
    which must close it on a byte boundary."""
    if not data or data[0] != hdlc.FLAG:
        return None
    at = 0
    while at < len(data) and data[at] == hdlc.FLAG:
        at += 1
    shift = 0
    out = bytearray()
    byte = nbits = 0
    for value in data[at:]:
        for i in range(8):
            bit = (value >> i) & 1
            shift = ((shift >> 1) | (bit << 7)) & 0xFF
            if shift == 0xFE:
                return None                    # seven ones: not a frame
            if bit == 0:
                if shift == hdlc.FLAG:
                    return bytes(out) if nbits == 7 else None
                if (shift >> 2) == 0x1F:
                    continue                    # the stuffed zero
            byte = (byte >> 1) | (bit << 7)
            nbits += 1
            if nbits == 8:
                out.append(byte)
                byte = nbits = 0
    return None


@dataclass
class Decoded:
    frame: bytes          # without its FCS
    tag: int
    corrected: int        # bytes the RS code put right
    end_bit: int          # the bit count at which the block ended
    start_bit: int        # where the frame's opening flag ended


class Fx25Receiver:
    """NRZI-decoded bits in, corrected frames out. Streams: a block straddles
    any number of calls. Bits arrive as numpy arrays of 0 and 1; the tag
    search runs on the whole array at once."""

    # float32, not int: numpy's integer matrix product is not BLAS's, and on
    # a 100 ms block it was 40 us a slicer against 5 (shack, 17/10/2026).
    # The scores are small integers, exact in float32.
    _MATRIX = np.array([[1 if b else -1 for b in t.bits] for t in TAGS.values()], dtype=np.float32).T
    _NUMBERS = list(TAGS)
    _THRESHOLD = 64 - 2 * TAG_TOLERANCE

    def __init__(self):
        self._hist = np.zeros(0, dtype=np.int8)      # the last 63 bits, for a tag across a join
        self._collect: Tag | None = None
        self._got: list[np.ndarray] = []
        self._have = 0
        self._block_start = 0
        self.count = 0                                  # bits seen, for dating
        self.tags = 0
        self.failed = 0

    @property
    def busy(self) -> bool:
        """Part-way through a block (the receiver holds plain copies for it)."""
        return self._collect is not None

    def reset(self) -> None:
        """Abandon a block part-collected (the audio has stopped)."""
        self._collect = None
        self._got, self._have = [], 0

    def feed(self, bits: np.ndarray) -> list[Decoded]:
        out: list[Decoded] = []
        bits = np.asarray(bits, dtype=np.int8)
        at = 0
        while at < len(bits):
            if self._collect is not None:
                want = (self._collect.block * 8) - self._have
                take = bits[at:at + want]
                self._got.append(take)
                self._have += len(take)
                at += len(take)
                if self._have == self._collect.block * 8:
                    got = self._finish(self.count + at)
                    if got is not None:
                        out.append(got)
                    self._hist = np.zeros(0, dtype=np.int8)
                continue
            seg = np.concatenate([self._hist, bits[at:]])
            if len(seg) >= 64:
                win = np.lib.stride_tricks.sliding_window_view(seg.astype(np.float32) * 2 - 1, 64)
                score = win @ self._MATRIX
                hits = np.argwhere(score >= self._THRESHOLD)
                if len(hits):
                    start, which = hits[np.argmin(hits[:, 0])]
                    end = int(start) + 64                     # index in seg after the tag
                    self._collect = TAGS[self._NUMBERS[int(which)]]
                    self.tags += 1
                    self._got, self._have = [], 0
                    used = end - len(self._hist)               # index in bits[at:]
                    at += used
                    self._block_start = self.count + at
                    continue
            self._hist = seg[-63:]
            at = len(bits)
        self.count += len(bits)
        return out

    def _finish(self, end_bit: int) -> Decoded | None:
        tag = self._collect
        self._collect = None
        raw = np.concatenate(self._got)
        self._got, self._have = [], 0
        weights = (1 << np.arange(8)).astype(np.int64)
        received = (raw.reshape(-1, 8).astype(np.int64) * weights).sum(axis=1)
        rs = codec(tag.nroots)
        block = bytearray(NN)
        block[:tag.data] = bytes(received[:tag.data].tolist())
        block[rs.k:] = bytes(received[tag.data:].tolist())
        fixed = rs.decode(bytes(block), forbidden=range(tag.data, rs.k))
        if fixed is None:
            self.failed += 1
            return None
        corrected, positions = fixed
        data = corrected[:tag.data]
        frame = unstuff_block(data)
        if frame is None or len(frame) < hdlc.MIN_FRAME or not hdlc.fcs_ok(frame):
            self.failed += 1
            return None
        lead = 0
        while lead < len(data) and data[lead] == hdlc.FLAG:
            lead += 1
        return Decoded(frame=frame[:-2], tag=tag.number, corrected=len(positions),
                       end_bit=end_bit, start_bit=self._block_start + lead * 8)
