"""How bytes reach a device: one small interface, several ways to open it.

A driver (drivers/) knows a model's protocol and nothing about the
operating system; a transport knows the operating system and nothing about
the protocol. That split is what lets the AKP03E driver run unchanged over
Linux hidraw (the controller and Linux desktops, as the box does today),
over the hidapi package (Windows and macOS), and over shack_pad.fake.FakeHid
in the tests.

The interface, duck-typed so the fake needs no base class:

- read(timeout) -> bytes | None: one report (or one evdev read), None when
  nothing arrived in timeout seconds. Raises OSError when the device has
  gone - unplugged, a USB reset, hub power - and that OSError is the signal
  the pad layer acts on (CONTROLLER-PAD.md §7.2: unkey first, then close).
- write(data): one output report, already framed by the driver (for HID,
  including the leading report id byte).
- close(): idempotent.
"""

from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    path: str

    def read(self, timeout: float) -> bytes | None: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...
