"""Building test input by hand.

Written here rather than taken from ax25.py on purpose: a test that builds
its input with the same code that reads it passes when both are wrong in the
same way. (The ax25 tests check the encoder against these bytes.)
"""

from __future__ import annotations

import shutil

import pytest

from shack_packet import hdlc


def address(call: str, ssid: int = 0, last: bool = False, flag: bool = False) -> bytes:
    """A seven-byte AX.25 address."""
    out = bytearray(ord(ch) << 1 for ch in call.ljust(6))
    out.append(0x60 | (ssid << 1) | (1 if last else 0) | (0x80 if flag else 0))
    return bytes(out)


def frame_bytes(source: str, destination: str, info: str, via=None, info_bytes: bytes | None = None,
                control: int = 0x03, pid: int | None = 0xF0) -> bytes:
    """A whole frame without its FCS, from the parts."""
    def one(spec: str, last: bool):
        call, _, ssid = spec.rstrip("*").partition("-")
        return address(call, int(ssid or 0), last=last, flag=spec.endswith("*"))

    via = via or []
    body = one(destination, False) + one(source, not via)
    for i, hop in enumerate(via):
        body += one(hop, i == len(via) - 1)
    body += bytes([control]) + (bytes([pid]) if pid is not None else b"")
    return body + (info_bytes if info_bytes is not None else info.encode("latin-1"))


def on_air(frame: bytes) -> bytes:
    """The frame with its FCS, as the Framer sees it before checking."""
    return frame + hdlc.fcs(frame).to_bytes(2, "little")


def need(binary: str) -> str:
    where = shutil.which(binary)
    if not where:
        pytest.skip(f"{binary} is not installed; direwolf is a development tool, not a dependency")
    return where
