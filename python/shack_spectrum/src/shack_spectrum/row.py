"""The spectrum/1 binary row (CLIENT-UI-PLAN.md §2.3).

A row is 32 bytes of little-endian header and then n columns, uint8 or
uint16. It is binary rather than JSON because it is the one message sent
at frame rate to every client: 1280 columns at 15 fps is ~20 kB/s, where
Radio's base64 JSON feed is ~110 kB/s for fewer columns.

The header is fixed: a reader checks magic and version and refuses
anything else rather than guessing, because a mis-read header draws a
plausible but wrong waterfall, which is worse than none. Flags a reader
does not know (bits 4-7) are ignored, so a later spectrum/1 may add one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"OS"
VERSION = 1
HEADER = struct.Struct("<2sBBHHIQdf")
assert HEADER.size == 32

DEPTH16 = 0x01
LEVELS_CHANGED = 0x02
STREAM_CHANGED = 0x04
GAP = 0x08


class SpectrumError(Exception):
    """Misuse of this library: a bug report for the app's developer, never
    a sentence for an operator (so not a ValueError, which ohs-libs'
    i18n check treats as user-facing). A client's bad subscribe is caught
    earlier, by spectrum.schema.json, and answered with an error message."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


class RowError(SpectrumError):
    """A row that must not be drawn. `code` is the name the vectors use:
    magic, version, length."""


@dataclass
class Row:
    stream: int
    seq: int
    t_ms: int
    lo_hz: float
    col_hz: float
    values: np.ndarray = field(repr=False)
    levels_changed: bool = False
    stream_changed: bool = False
    gap: bool = False

    @property
    def depth(self) -> int:
        return 16 if self.values.dtype == np.uint16 else 8

    @property
    def flags(self) -> int:
        return ((DEPTH16 if self.depth == 16 else 0) | (LEVELS_CHANGED if self.levels_changed else 0)
                | (STREAM_CHANGED if self.stream_changed else 0) | (GAP if self.gap else 0))


def encode(row: Row) -> bytes:
    v = np.asarray(row.values)
    if v.dtype not in (np.uint8, np.uint16):
        raise SpectrumError("depth", f"values must be uint8 or uint16, not {v.dtype}")
    if v.ndim != 1 or len(v) > 0xFFFF:
        raise SpectrumError("columns", "values must be one row of at most 65535 columns")
    head = HEADER.pack(MAGIC, VERSION, row.flags, row.stream, len(v), row.seq & 0xFFFFFFFF,
                       row.t_ms, row.lo_hz, row.col_hz)
    return head + v.astype(v.dtype.newbyteorder("<"), copy=False).tobytes()


def decode(data: bytes) -> Row:
    if len(data) < HEADER.size:
        raise RowError("length", f"{len(data)} bytes is shorter than the header")
    magic, version, flags, stream, n, seq, t_ms, lo_hz, col_hz = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise RowError("magic", f"{magic!r}")
    if version != VERSION:
        raise RowError("version", f"version {version}; this reader knows {VERSION}")
    dtype = np.dtype("<u2") if flags & DEPTH16 else np.dtype("u1")
    want = HEADER.size + n * dtype.itemsize
    # Exact length, not at-least: a WebSocket frame is one row, so trailing
    # bytes mean the writer and reader disagree on the layout.
    if len(data) != want:
        raise RowError("length", f"{len(data)} bytes for {n} columns of {dtype.itemsize}; want {want}")
    values = np.frombuffer(data, dtype=dtype, count=n, offset=HEADER.size).astype(
        np.uint16 if flags & DEPTH16 else np.uint8)
    return Row(stream, seq, t_ms, lo_hz, col_hz, values, levels_changed=bool(flags & LEVELS_CHANGED),
               stream_changed=bool(flags & STREAM_CHANGED), gap=bool(flags & GAP))
