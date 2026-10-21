"""A fake HID device for tests: replays reports and records writes.

Stands in for a transport (see transport/__init__.py), so a driver and the
whole input host run in CI with no pad attached (CONTROLLER-PAD.md §9). It
is a transport rather than a fake driver on purpose: the driver's own
framing, decoding and changed-keys cache are what the tests are about.

    dev = FakeHid()
    dev.push(akp03e_report(0x01, 1))     # K1 down, as the pad sends it
    dev.unplug()                         # the next read raises OSError
    dev.writes                           # every report the driver wrote

Reads block for up to their timeout waiting for a push, so a test may feed
a host running on another thread; with nothing queued and no thread, a read
simply times out (None), as a quiet pad does.
"""

from __future__ import annotations

import threading
import time


class FakeHid:
    def __init__(self, reports: list[bytes] | None = None, path: str = "fake:0"):
        self.path = path
        self.queue: list[bytes] = list(reports or [])
        self.writes: list[bytes] = []
        self.gone = False
        self.closed = False
        self.reads = 0
        self.cond = threading.Condition()

    # Test side -----------------------------------------------------------------------
    def push(self, *reports: bytes) -> None:
        with self.cond:
            self.queue.extend(reports)
            self.cond.notify_all()

    def unplug(self) -> None:
        """From now on the device is gone: reads and writes raise OSError,
        as hidraw does when the cable is pulled."""
        with self.cond:
            self.gone = True
            self.cond.notify_all()

    def drained(self) -> bool:
        with self.cond:
            return not self.queue

    # Transport side ------------------------------------------------------------------
    def read(self, timeout: float) -> bytes | None:
        end = time.monotonic() + timeout
        with self.cond:
            self.reads += 1
            while True:
                if self.queue:
                    return self.queue.pop(0)
                if self.gone or self.closed:
                    raise OSError(19, "No such device")
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def write(self, data: bytes) -> None:
        with self.cond:
            if self.gone or self.closed:
                raise OSError(19, "No such device")
            self.writes.append(bytes(data))

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def akp03e_report(code: int, state: int = 0, size: int = 512) -> bytes:
    """An AKP03E input report as captured from workshop's pad (3 Oct 2026):
    ACK\\0\\0OK, two zero bytes, the control code, then 1/0 for press/release."""
    return (b"ACK\0\0OK\0\0" + bytes([code, state])).ljust(size, b"\0")


def evdev_events(*events: tuple[int, int, int]) -> bytes:
    """struct input_event records, (type, code, value) each, as a keypad's
    event node returns them on 64-bit Linux."""
    import struct  # noqa: PLC0415
    ev = struct.Struct("llHHi")
    return b"".join(ev.pack(0, 0, t, c, v) for t, c, v in events)
