"""A radio made of a pty, and a by-id directory to find it in.

Moved out of OpenHamShack Icom's tools/selftest_usb.py so test_usb.py reads
as the checks rather than the scaffolding. `openpty()` gives two ends of a
terminal; a fake IC-7610 sits on one and the library's own `SerialPort`,
`probe` and `UsbLink` sit on the other, so the port is opened for real, the
termios is set for real, the locks are taken for real and the frames are split
across reads the way a serial port really splits them.

**No radio is touched.** Nothing here opens a device under /dev/serial;
`usbprobe.BY_ID` is pointed at a pytest temporary directory of symlinks named
exactly as the real ones are, which is also what makes the by-id recognition
testable.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from shack_rig.icom import civ, usbprobe

#: The real string, from `ls -l /dev/serial/by-id` on `shack` with the IC-7610
#: plugged in. Used verbatim because half of what the probe does with it -
#: finding "IC-7610" in it, and reading the _A suffix - is parsing this exact
#: shape, and a tidied-up version of it would test the tidying.
SEVEN_A = ("usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller"
           "_IC-7610_23002583_A-if00-port0")
SEVEN_B = ("usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller"
           "_IC-7610_23002583_B-if00-port0")

SEVEN_ADDR = 0x98


class FakeRadio:
    """A radio on the far end of a pty, answering CI-V the way a 7610 does.

    Modelled on what the real one actually did rather than on the manual, and
    the two differ in the place that matters: **it does not answer a frame
    addressed to 0x00**. CI-V documents 0x00 as a broadcast and the obvious
    probe would use it, which would have made the address sweep unnecessary.
    Measured on the IC-7610 three times over: `FE FE 00 E0 19 00 FD` is ignored
    on a link that answers the identical question at 0x98 every time. A fake
    that was more generous than the radio would let a probe that cannot work
    pass these tests.
    """

    def __init__(self, fd: int, address: int = SEVEN_ADDR,
                 freq: int = 7_164_437, answer: bool = True,
                 echo: bool = False):
        self.fd = fd
        self.address = address
        self.freq = freq
        self.answer = answer
        self.echo = echo
        self.heard: list[bytes] = []
        self.split = 0            #: reply in two writes, the first this many bytes
        self._buf = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = os.read(self.fd, 256)
            except OSError:
                return
            if not data:
                continue
            self._buf += data
            frames, self._buf = civ.split_frames(self._buf)
            self._handle(frames)

    def _handle(self, frames: list[bytes]) -> None:
        """Answer everything that arrived. `Deaf` in test_usb.py does not."""
        for f in frames:
            self._reply(f)

    def _reply(self, f: bytes) -> None:
        try:
            to, frm, payload = civ.parse(f)
        except civ.CivError:
            return
        self.heard.append(f)
        if self.echo:
            self._write(f)
        if not self.answer or to != self.address or not payload:
            return
        if payload[0] == 0x19:
            body = bytes([0x19, 0x00, self.address])
        elif payload[0] == 0x03:
            body = bytes([0x03]) + civ.encode_freq(self.freq)
        elif payload[0] == 0x26:
            body = bytes([0x26, 0x00, civ.MODE_CODES["USB"], 0x00, 0x01])
        elif payload[0] == 0x1C:
            body = bytes([0x1C, 0x00, 0x00])
        elif payload[0] == 0x15 and len(payload) > 1 and payload[1] == 0x02:
            # An S-meter reading, BCD, as the real one answers it.
            body = bytes([0x15, 0x02, 0x00, 0x42])
        else:
            return
        self._write(civ.frame(body, to=frm, frm=self.address))

    def _write(self, out: bytes) -> None:
        if self.split:
            # A serial port splits a frame across two reads as a matter of
            # course, and the caller's buffer is what has to cope. Reproduced
            # here on purpose rather than hoped for.
            os.write(self.fd, out[:self.split])
            time.sleep(0.02)
            os.write(self.fd, out[self.split:])
        else:
            os.write(self.fd, out)


class Bench:
    """A directory of by-id symlinks pointing at ptys, as the host would have.

    `usbprobe.BY_ID` is redirected here (through pytest's monkeypatch, so it is
    put back however the test ends). That is the whole of the mocking:
    everything below it - the glob, the realpath, the model recognised out of
    the name, the `_A` suffix, the open, the termios, the locks - is the real
    code doing the real thing to a real descriptor.

        with Bench([SEVEN_A], tmp_path / "byid", monkeypatch) as bench: ...
    """

    def __init__(self, names: list[str], where: Path, monkeypatch):
        where.mkdir(parents=True)
        self.dir = where
        self.ports: dict[str, int] = {}          #: name -> the master fd
        self.paths: dict[str, str] = {}
        # The slave ends stay open here so the ptys survive; SerialPort opens
        # the same path again by name, which is what the library does.
        self._keep: list[int] = []
        for name in names:
            master, slave = os.openpty()
            os.symlink(os.ttyname(slave), where / name)
            self.ports[name] = master
            self.paths[name] = str(where / name)
            self._keep.append(slave)
        monkeypatch.setattr(usbprobe, "BY_ID", str(where))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for fd in list(self.ports.values()) + self._keep:
            try:
                os.close(fd)
            except OSError:
                pass
