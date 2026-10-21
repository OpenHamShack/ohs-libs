"""A streaming FLAC encoder for 16-bit receive audio, in numpy.

**Why FLAC** (Brian, 12/10/2026, chose "FLAC or Opus/OGG, WAV if simplest"):

- *Lossless.* A recording is kept to be listened to again, decoded again
  (a CW or FT8 decoder run over it later) or sent to somebody. Opus throws
  away what a psychoacoustic model says a person will not miss. That is
  right for speech, and wrong for a weak carrier under the noise, which is
  what a radio recording is often for.
- *Every player can play it.* Chrome, Firefox and Safari (macOS 11, iOS 11)
  play FLAC in an `<audio>` element; Android's MediaPlayer and ExoPlayer and
  Apple's AVPlayer play it. So the box page and the Remote need no codec of
  their own.
- *A crash still leaves a playable file.* FLAC allows "length unknown" in
  its header, and frames carry their own sync and CRCs, so a file cut off
  mid-write plays up to the cut. A WAV whose header was never finished says
  it holds no samples.
- *Half the size of WAV or less* on receive audio (silence costs almost
  nothing).

**Why our own encoder.** The rig apps' images carry numpy and no codec
library, and the Icom app also runs on a Pi and as a desktop bundle. libFLAC
through ctypes, or python3-soundfile, would add a native dependency to three
images and two desktop packagings for one feature. FLAC's compressing forms
are small: fixed polynomial predictors (orders 0-4, numpy's `diff`), linear
prediction (LPC) with quantised coefficients, and Rice-coded residuals. LPC
is what makes it worth having: receive audio is band-limited (300-2700 Hz
in a 12 kHz stream), which a fixed predictor cannot exploit. Measured on
300-2700 Hz noise at 12 kHz (12/10/2026): fixed predictors alone 63-84 %
of WAV, with LPC 36-50 % (ffmpeg's encoder at level 8: 40-53 %); 24 kHz
stereo 23-31 %. CPU on ohs-dev: 9 ms per second of 12 kHz mono, 35 ms per
second of 24 kHz stereo. `tests/flacref.py` decodes what this writes, and
`test_flac.py` also has ffmpeg check it when ffmpeg is installed.

What a file holds: STREAMINFO, then a VORBIS_COMMENT block with the
recording's tags (frequency, mode, station, start time), then frames of
4096 samples a channel. Each channel is coded independently; no
mid/side, because the apps record mono except Flex's stereo slice audio.
The total length and the MD5 in STREAMINFO are written in by `close()`.
Until then they say "unknown", which every decoder accepts.
"""
from __future__ import annotations

import hashlib
import struct
from typing import BinaryIO

import numpy as np

BLOCK = 4096                     # samples a channel in every frame but the last
BITS = 16
MAX_ORDER = 4                    # FLAC's fixed predictors
MAX_PARTITION_ORDER = 4          # 16 partitions: past that the 5-bit headers eat the gain on radio audio
LPC_ORDERS = (2, 4, 8, 12)       # tried after the fixed predictors; 12 is where band-limited audio stops gaining
LPC_PRECISION = 14               # bits a quantised coefficient takes (libFLAC uses 12-15 at these orders)
RICE_MAX = 30                    # RICE2's 5-bit parameter; 31 is the escape code
VENDOR = "OpenHamShack shack_rec"

# Sample rates the frame header can name with a code of its own.
_RATE_CODES = {88200: 1, 176400: 2, 192000: 3, 8000: 4, 16000: 5, 22050: 6, 24000: 7, 32000: 8,
               44100: 9, 48000: 10, 96000: 11}


class FlacMisuse(ValueError):
    """A caller's mistake (a channel count or rate FLAC cannot carry, a
    block with the wrong number of channels): for the developer reading a
    traceback, never a sentence shown to a person, so not translated. The
    apps build the writer from the radio's own format."""


def _crc_table(poly: int, width: int) -> list[int]:
    top, mask = 1 << (width - 1), (1 << width) - 1
    out = []
    for i in range(256):
        c = i << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) if c & top else (c << 1)
        out.append(c & mask)
    return out


_CRC8 = _crc_table(0x07, 8)
_CRC16 = _crc_table(0x8005, 16)


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


def crc16(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC16[(c >> 8) ^ b]
    return c


def _utf8_number(v: int) -> bytes:
    """The frame number, coded as FLAC codes it: UTF-8's scheme stretched to 31 bits."""
    if v < 0x80:
        return bytes([v])
    for n, limit in ((2, 0x800), (3, 0x10000), (4, 0x200000), (5, 0x4000000), (6, 0x80000000)):
        if v < limit:
            tail = []
            for _ in range(n - 1):
                tail.append(0x80 | (v & 0x3F))
                v >>= 6
            return bytes([((0xFF << (8 - n)) & 0xFF) | v] + tail[::-1])
    raise FlacMisuse("frame number past FLAC's 31 bits")


def _bits(values, width: int) -> np.ndarray:
    """Values (two's complement when negative) as `width` bits each, MSB first."""
    v = np.atleast_1d(np.asarray(values, np.int64)) & ((1 << width) - 1)
    return ((v[:, None] >> np.arange(width - 1, -1, -1, dtype=np.int64)) & 1).astype(np.uint8).ravel()


def _zigzag(r: np.ndarray) -> np.ndarray:
    return np.where(r >= 0, r << 1, ((-r) << 1) - 1)


def _rice_cost(u: np.ndarray) -> tuple[int, int]:
    """(best parameter, bits) for folded residuals `u`. The cost is convex
    in the parameter, so walk up from the estimate's floor until it rises."""
    n = len(u)
    if n == 0:
        return 0, 0
    mean = float(u.mean())
    k = max(0, min(RICE_MAX, int(np.log2(mean)) - 1 if mean >= 2 else 0))
    best_k, best = k, int((u >> k).sum()) + n * (1 + k)
    while best_k < RICE_MAX:
        k = best_k + 1
        cost = int((u >> k).sum()) + n * (1 + k)
        if cost >= best:
            break
        best_k, best = k, cost
    return best_k, best


def _rice_bits(u: np.ndarray, k: int) -> np.ndarray:
    """Folded residuals as Rice codes: q zeros, a one, then k bits of remainder."""
    if len(u) == 0:
        return np.zeros(0, np.uint8)
    q = u >> k
    length = q + 1 + k
    ends = np.cumsum(length)
    starts = ends - length
    out = np.zeros(int(ends[-1]), np.uint8)
    out[starts + q] = 1
    if k:
        rem = u & ((1 << k) - 1)
        pos = (starts + q + 1)[:, None] + np.arange(k, dtype=np.int64)
        out[pos.ravel()] = ((rem[:, None] >> np.arange(k - 1, -1, -1, dtype=np.int64)) & 1).ravel()
    return out


def _residual_plan(u: np.ndarray, n: int, order: int) -> tuple[int, int, list[tuple[int, int]]]:
    """The cheapest partitioning of a block's folded residuals (`u`, n -
    order of them): (partition order, bits including the 6-bit method and
    order fields, [(start, parameter)...])."""
    best = None
    for p in range(MAX_PARTITION_ORDER + 1):
        if n % (1 << p) or (n >> p) < order:
            break
        size = n >> p
        parts, bits, start = [], 2 + 4, 0
        for i in range(1 << p):
            count = size - order if i == 0 else size
            k, cost = _rice_cost(u[start:start + count])
            parts.append((start, k))
            bits += 5 + cost
            start += count
        if best is None or bits < best[1]:
            best = (p, bits, parts)
    return best


def _lpc(x: np.ndarray, max_order: int) -> list[np.ndarray]:
    """Linear prediction coefficients for orders 1..max_order (Levinson-
    Durbin on the Tukey-windowed autocorrelation, as libFLAC does), each as
    a[j] such that x[n] ~ sum a[j] x[n-1-j]."""
    n = len(x)
    w = np.ones(n)
    edge = n // 4                                     # Tukey(0.5): a quarter tapered at each end
    if edge:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(edge) / edge)
        w[:edge], w[-edge:] = ramp, ramp[::-1]
    y = x * w
    r = np.array([np.dot(y[:n - k], y[k:]) for k in range(max_order + 1)])
    if r[0] <= 0:
        return []
    r[0] *= 1 + 1e-9                                  # a whisker of white noise keeps it well conditioned
    out, a, err = [], np.zeros(0), r[0]
    for i in range(max_order):
        k = (r[i + 1] - np.dot(a, r[i:0:-1])) / err
        a = np.concatenate([a - k * a[::-1], [k]])
        err *= 1 - k * k
        out.append(a.copy())
        if err <= 0:
            break
    return out


def _quantise(a: np.ndarray) -> tuple[np.ndarray, int] | None:
    """Coefficients as LPC_PRECISION-bit integers and the shift that scales
    them back, rounding with error feedback as libFLAC does."""
    cmax = float(np.max(np.abs(a)))
    if not np.isfinite(cmax) or cmax == 0:
        return None
    shift = LPC_PRECISION - 1 - (int(np.floor(np.log2(cmax))) + 1)
    shift = max(0, min(15, shift))
    lim = (1 << (LPC_PRECISION - 1)) - 1
    q, err = np.zeros(len(a), np.int64), 0.0
    for j, c in enumerate(a):
        err += c * (1 << shift)
        q[j] = max(-lim - 1, min(lim, int(round(err))))
        err -= q[j]
    return q, shift


def _lpc_residual(x: np.ndarray, q: np.ndarray, shift: int) -> np.ndarray:
    p, n = len(q), len(x)
    pred = np.zeros(n - p, np.int64)
    for j in range(p):
        pred += q[j] * x[p - 1 - j:n - 1 - j]
    return x[p:] - (pred >> shift)


def _subframe(x: np.ndarray) -> np.ndarray:
    """One channel of one frame as the cheapest of CONSTANT, FIXED 0-4,
    LPC and VERBATIM, as bits. Candidates are ranked by a one-partition
    Rice cost (two vector operations each); only the winner is partitioned."""
    n = len(x)
    if n and np.all(x == x[0]):
        return np.concatenate([_bits(0b00000000, 8), _bits(int(x[0]), BITS)])
    cands = []                                         # (estimated bits, header bits, warm-up, folded residual)
    for order in range(min(MAX_ORDER, n - 1) + 1):
        u = _zigzag(np.diff(x, order) if order else x)
        head = [_bits(0b00010000 | (order << 1), 8)]
        cands.append((_rice_cost(u)[1] + 8 + BITS * order, head, order, u))
    if n > 2 * LPC_ORDERS[-1]:
        lp = _lpc(x.astype(np.float64), LPC_ORDERS[-1])
        for order in LPC_ORDERS:
            if order > len(lp):
                break
            qs = _quantise(lp[order - 1])
            if qs is None:
                continue
            q, shift = qs
            r = _lpc_residual(x, q, shift)
            if np.abs(r).max() >= 1 << 30:
                continue
            u = _zigzag(r)
            head = [_bits(0b01000000 | ((order - 1) << 1), 8)]
            tail = [_bits(LPC_PRECISION - 1, 4), _bits(shift, 5), _bits(q, LPC_PRECISION)]
            bits = _rice_cost(u)[1] + 8 + BITS * order + 9 + LPC_PRECISION * order
            cands.append((bits, head, order, u, tail))
    best = min(cands, key=lambda c: c[0])
    _est, head, order, u = best[:4]
    p, bits, parts = _residual_plan(u, n, order)
    if bits + _est - _rice_cost(u)[1] >= 8 + BITS * n:
        return np.concatenate([_bits(0b00000010, 8), _bits(x, BITS)])
    out = list(head)
    if order:
        out.append(_bits(x[:order], BITS))
    if len(best) > 4:
        out += best[4]
    # Residual coding method 1 (RICE2, 5-bit parameters): a parameter can
    # pass 14 on a loud, wideband block, which RICE (4-bit) cannot code.
    out += [_bits(1, 2), _bits(p, 4)]
    bounds = [s for s, _k in parts] + [len(u)]
    for i, (start, k) in enumerate(parts):
        out.append(_bits(k, 5))
        out.append(_rice_bits(u[start:bounds[i + 1]], k))
    return np.concatenate(out)


class FlacWriter:
    """FLAC to a seekable binary file, a block at a time.

    `write()` takes int16 samples, shape (n,) for mono or (n, channels),
    in any amounts; whole frames are encoded as they fill. `close()`
    encodes what is left and writes the length, frame sizes and MD5 into
    STREAMINFO. The file is not closed: it belongs to the caller."""

    def __init__(self, f: BinaryIO, rate: int, channels: int = 1, tags: dict | None = None):
        if not 1 <= channels <= 8:
            raise FlacMisuse("FLAC carries 1 to 8 channels")
        if not 1 <= rate < 1 << 20:
            raise FlacMisuse("FLAC sample rates are 1 Hz to 1 MHz")
        self.f, self.rate, self.channels = f, int(rate), int(channels)
        self.samples = 0                     # per channel, in frames written
        self.frames = 0
        self.min_frame = 0
        self.max_frame = 0
        self.md5 = hashlib.md5()
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._rate_code, self._rate_extra = self._rate_header(self.rate)
        self.f.write(b"fLaC")
        self._streaminfo_at = self.f.tell() + 4
        self.f.write(self._block_header(0, False, 34) + self._streaminfo())
        comment = self._vorbis_comment(tags or {})
        self.f.write(self._block_header(4, True, len(comment)) + comment)
        self.bytes = self.f.tell()

    @staticmethod
    def _rate_header(rate: int) -> tuple[int, bytes]:
        if rate in _RATE_CODES:
            return _RATE_CODES[rate], b""
        if rate % 1000 == 0 and rate // 1000 < 256:
            return 12, bytes([rate // 1000])
        if rate < 65536:
            return 13, struct.pack(">H", rate)
        if rate % 10 == 0 and rate // 10 < 65536:
            return 14, struct.pack(">H", rate // 10)
        return 0, b""                         # "see STREAMINFO"

    @staticmethod
    def _block_header(kind: int, last: bool, length: int) -> bytes:
        return bytes([(0x80 if last else 0) | kind]) + length.to_bytes(3, "big")

    def _streaminfo(self, final: bool = False) -> bytes:
        packed = ((self.rate << 44) | ((self.channels - 1) << 41) | ((BITS - 1) << 36)
                  | (self.samples if final else 0))
        return (struct.pack(">HH", BLOCK, BLOCK) + self.min_frame.to_bytes(3, "big")
                + self.max_frame.to_bytes(3, "big") + packed.to_bytes(8, "big")
                + (self.md5.digest() if final else bytes(16)))

    @staticmethod
    def _vorbis_comment(tags: dict) -> bytes:
        vendor = VENDOR.encode()
        items = [f"{str(k).upper()}={v}".encode() for k, v in tags.items() if v not in (None, "")]
        out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(items))
        for item in items:
            out += struct.pack("<I", len(item)) + item
        return out

    def write(self, samples) -> None:
        x = np.asarray(samples, np.int16)
        if x.ndim == 1:
            x = x.reshape(-1, 1) if self.channels == 1 else x.reshape(-1, self.channels)
        if x.shape[1] != self.channels:
            raise FlacMisuse(f"expected {self.channels} channels, got {x.shape[1]}")
        if not len(x):
            return
        self._pending.append(x)
        self._pending_n += len(x)
        if self._pending_n < BLOCK:
            return
        buf = np.concatenate(self._pending)
        whole = len(buf) // BLOCK * BLOCK
        for i in range(0, whole, BLOCK):
            self._frame(buf[i:i + BLOCK])
        rest = buf[whole:]
        self._pending = [rest] if len(rest) else []
        self._pending_n = len(rest)

    def _frame(self, x: np.ndarray) -> None:
        n = len(x)
        head = bytearray(b"\xff\xf8")         # sync, reserved, fixed block size
        bs_code, bs_extra = (12, b"") if n == BLOCK else (7, struct.pack(">H", n - 1))
        head.append((bs_code << 4) | self._rate_code)
        head.append(((self.channels - 1) << 4) | (0b100 << 1))     # independent channels, 16 bits
        head += _utf8_number(self.frames) + bs_extra + self._rate_extra
        head.append(crc8(head))
        wide = x.astype(np.int64)
        bits = np.concatenate([_subframe(wide[:, c]) for c in range(self.channels)])
        body = np.packbits(bits).tobytes()   # pads the last byte with zeros, as FLAC wants
        frame = bytes(head) + body
        frame += struct.pack(">H", crc16(frame))
        self.f.write(frame)
        self.md5.update(np.ascontiguousarray(x).astype("<i2").tobytes())
        self.min_frame = len(frame) if not self.frames else min(self.min_frame, len(frame))
        self.max_frame = max(self.max_frame, len(frame))
        self.frames += 1
        self.samples += n
        self.bytes += len(frame)

    def close(self) -> None:
        """Encode what is left and write the totals into STREAMINFO."""
        if self._pending_n:
            self._frame(np.concatenate(self._pending))
            self._pending, self._pending_n = [], 0
        end = self.f.tell()
        self.f.seek(self._streaminfo_at)
        self.f.write(self._streaminfo(final=True))
        self.f.seek(end)
        self.f.flush()
