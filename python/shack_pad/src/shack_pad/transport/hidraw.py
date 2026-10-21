"""Linux hidraw: the pad's node opened directly, exactly as the box does.

Stdlib only, so the controller needs no extra package. The udev rule
(udev/91-openhamshack-pad.rules) gives the node to group shack-input, so the
input host reads it without root.
"""

from __future__ import annotations

import os
import select
from pathlib import Path


class HidrawTransport:
    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def read(self, timeout: float) -> bytes | None:
        if self.fd is None:
            raise OSError("closed")
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self.fd, 1024)
        if not data:
            # A hidraw node that has lost its device reads as end of file on
            # some kernels rather than raising; both mean the same thing.
            raise OSError("device gone")
        return data

    def write(self, data: bytes) -> None:
        if self.fd is None:
            raise OSError("closed")
        os.write(self.fd, data)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def find(hid_id: str, interface: str = "input0", sysfs: Path = Path("/sys/class/hidraw")) -> list[str]:
    """hidraw nodes whose device is hid_id (bus:vendor:product as in
    uevent's HID_ID, e.g. 0003:00000300:00003002) on the given interface.
    The AKP03E shows two: input0 is its vendor interface, input1 a keyboard
    face that must never be opened as the pad."""
    try:
        nodes = sorted(sysfs.iterdir())
    except OSError:
        return []
    out = []
    for node in nodes:
        try:
            uevent = (node / "device" / "uevent").read_text()
        except OSError:
            continue
        if f"HID_ID={hid_id}" in uevent and interface in uevent:
            out.append(f"/dev/{node.name}")
    return out
