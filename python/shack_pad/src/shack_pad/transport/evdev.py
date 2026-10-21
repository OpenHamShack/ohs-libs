"""Linux evdev: a USB keypad's event node, grabbed while we read it.

Stdlib only, as on the box (controllers/keypad.py). EVIOCGRAB makes the
keypad's keys reach nothing else while it is a controller, even if libinput
opened it first; closing the node releases the grab.

Windows and macOS have no equivalent grab, so there a keypad is read by the
focused app from its own window's key events and fed to the same map
(model `keyboard`, drivers/keypad.py).
"""

from __future__ import annotations

import os
import select

EVIOCGRAB = 0x40044590             # _IOW('E', 0x90, int)


class EvdevTransport:
    def __init__(self, path: str, grab: bool = True):
        import fcntl  # noqa: PLC0415  (POSIX only)
        self.path = path
        self.fd: int | None = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if grab:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            except OSError:
                os.close(self.fd)
                self.fd = None
                raise

    def read(self, timeout: float) -> bytes | None:
        if self.fd is None:
            raise OSError("closed")
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        raw = os.read(self.fd, 24 * 64)
        if not raw:
            raise OSError("device gone")
        return raw

    def write(self, data: bytes) -> None:
        raise OSError("a keypad has nothing to write to")

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
