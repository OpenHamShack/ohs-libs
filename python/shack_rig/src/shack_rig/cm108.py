"""PTT on a CM108/CM119 USB sound chip's GPIO pin.

The other way a sound-card interface keys a radio: no serial port at all,
one of the audio chip's own general-purpose pins pulled up through a
transistor to the radio's PTT (the DATA jack's PKS on a TM-V71). The
AllScan UCI and the many home-built CM108 boards do this; so do the
"DMK URI" family. (A Digirig keys by RTS on its own CP2102 serial port -
`serialio`, not this - although its audio is a CM108 too.)

Written from Dire Wolf's src/cm108.c (WB2OSZ), which is where the
community's knowledge of this lives:

- the pin is set by writing a 5-byte HID output report to the chip:
  `[0, 0, iodata, iomask, 0]` - report number 0, then a reserved byte, the
  output levels and the direction mask (1 = output), LSB being GPIO 1
  (`cm108_write`: io[0] = 0; io[1] = 0; io[2] = iodata; io[3] = iomask;
  io[4] = 0). The same five bytes go to Linux's /dev/hidrawN with write()
  and to hidapi's hid_write() elsewhere.
- GPIO 3 is the usual PTT pin ("Homebrew plans all use GPIO 3 because it
  is easier to tack solder a wire to a pin on the end"); GPIO 1-8 exist on
  some chips, 1, 3 and 4 on the CM108AH/B.
- C-Media's vendor ID is 0x0D8C, SSS's compatible chips 0x0C76, and the
  AIOC cable 0x1209:0x7388 (cm108.c's table of chips and IDs).

**Low on open.** Opening writes the pin as an output, low - "not keyed" -
before anything else, as serialio lowers RTS and DTR on open: a CM108 board
whose PTT pin floats at power-up is not left to whatever it reads as.
`set_line(name, on)` is the PTT driver's interface (the same as
serialio.SerialPort's), so a rig keys either kind of interface the same way;
the line name is ignored here - there is one pin.

**Permissions.** On Linux /dev/hidrawN is root's unless a udev rule gives
it to the user (Dire Wolf ships 99-direwolf-cmedia.rules for exactly this);
a refusal says so in a sentence rather than failing silently on the first
key.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
from pathlib import Path

from .i18n import _
from .serialio import SerialError

log = logging.getLogger("shack_rig.cm108")

#: Chips with a CM108-style GPIO report, from Dire Wolf cm108.c's table
#: (CMEDIA_VID, SSS_VID, AIOC_VID/AIOC_PID): any C-Media or SSS product, and
#: the AIOC by its own product ID - 0x1209 is pid.codes' shared vendor ID,
#: which hundreds of unrelated devices also carry.
GPIO_VENDORS = {0x0D8C, 0x0C76}
GPIO_PRODUCTS = {(0x1209, 0x7388)}


def gpio_capable(vid, pid) -> bool:
    return vid in GPIO_VENDORS or (vid, pid) in GPIO_PRODUCTS
DEFAULT_GPIO = 3


def report(gpio: int, on: bool) -> bytes:
    """The HID output report that sets `gpio` (1-8) as an output, high when
    `on` (Dire Wolf cm108_set_gpio_pin + cm108_write)."""
    if not 1 <= int(gpio) <= 8:
        raise ValueError(_("a CM108 GPIO number is 1 to 8, not {gpio}").format(gpio=gpio))
    mask = 1 << (int(gpio) - 1)
    return bytes([0, 0, mask if on else 0, mask, 0])


def find_hidraw(sys_root: str = "/sys/class/hidraw") -> list[dict]:
    """Linux: every hidraw device whose USB vendor is a GPIO-capable chip,
    as {"device": "/dev/hidraw3", "vid": .., "pid": .., "name": ..}."""
    out = []
    for path in sorted(glob.glob(os.path.join(sys_root, "hidraw*"))):
        try:
            uevent = Path(path, "device", "uevent").read_text()
        except OSError:
            continue
        vid = pid = None
        name = ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                try:
                    _bus, v, p = line.split("=", 1)[1].split(":")
                    vid, pid = int(v, 16), int(p, 16)
                except ValueError:
                    pass
            elif line.startswith("HID_NAME="):
                name = line.split("=", 1)[1]
        if gpio_capable(vid, pid):
            out.append({"device": "/dev/" + os.path.basename(path), "vid": vid, "pid": pid, "name": name})
    return out


def find() -> list[dict]:
    """Every CM108-style chip this computer can see: hidraw on Linux, hidapi
    (the `hid` module, when installed) elsewhere. Read-only."""
    if sys.platform.startswith("linux"):
        return find_hidraw()
    try:
        import hid                                  # noqa: PLC0415 - optional, desktop builds only
    except ImportError:
        return []
    out = []
    for d in hid.enumerate():
        if gpio_capable(d.get("vendor_id"), d.get("product_id")):
            path = d.get("path")
            out.append({"device": path.decode() if isinstance(path, bytes) else str(path),
                        "vid": d.get("vendor_id"), "pid": d.get("product_id"),
                        "name": d.get("product_string") or ""})
    return out


class Cm108Ptt:
    """One chip's PTT pin. `device` is a /dev/hidrawN path (Linux) or a
    hidapi path; "" picks the only CM108-style chip present, and refuses
    when there are none or several (guessing would key the wrong radio).
    `writer(device)` makes the object written to, for tests: anything with
    write(bytes) and close()."""

    def __init__(self, device: str = "", gpio: int = DEFAULT_GPIO, *, writer=None):
        report(gpio, False)                         # the number checked now, not at the first key
        self.device = device
        self.gpio = int(gpio)
        self._writer = writer
        self._out = None
        self.line_state = {"ptt": False}
        self.writes: int = 0

    def open(self) -> None:
        device = self.device
        if not device:
            found = find()
            if len(found) != 1:
                raise SerialError(_("{n} CM108-style sound chips are plugged in; choose the one wired to the "
                                    "radio's PTT").format(n=len(found)) if found else
                                  _("no CM108-style USB sound chip found for PTT"))
            device = self.device = found[0]["device"]
        try:
            self._out = (self._writer or _open_device)(device)
        except PermissionError:
            raise SerialError(_("{device}: permission denied. On Linux the sound chip's HID device needs a "
                                "udev rule giving it to your user (as Dire Wolf's 99-direwolf-cmedia.rules "
                                "does)").format(device=device)) from None
        except OSError as e:
            raise SerialError(_("{device} could not be opened for PTT: {why}").format(device=device,
                                                                                       why=e)) from None
        if not self._write(False):
            self.close()
            raise SerialError(_("{device} did not take the PTT pin's first write").format(device=device))

    def _write(self, on: bool) -> bool:
        out = self._out
        if out is None:
            return False
        try:
            out.write(report(self.gpio, on))
        except OSError as e:
            log.warning("CM108 PTT write on %s: %s", self.device, e)
            return False
        self.writes += 1
        self.line_state["ptt"] = bool(on)
        return True

    def set_line(self, name: str, on: bool) -> bool:
        """Key (True) or unkey. `name` is ignored: the chip has one PTT pin."""
        return self._write(bool(on))

    def close(self) -> None:
        out, self._out = self._out, None
        if out is None:
            return
        try:
            out.write(report(self.gpio, False))
            self.line_state["ptt"] = False
        except OSError:
            log.warning("CM108 PTT: the unkey on close did not go to %s", self.device)
        try:
            out.close()
        except OSError:
            pass


class _HidrawFile:
    """/dev/hidrawN, written unbuffered."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_WRONLY)

    def write(self, data: bytes) -> None:
        if os.write(self.fd, data) != len(data):
            raise OSError("short write")

    def close(self) -> None:
        os.close(self.fd)


class _HidapiDevice:
    def __init__(self, path: str):
        import hid                                  # noqa: PLC0415
        self.dev = hid.device()
        self.dev.open_path(path.encode() if isinstance(path, str) else path)

    def write(self, data: bytes) -> None:
        if self.dev.write(list(data)) < 0:
            raise OSError("hid write failed")

    def close(self) -> None:
        self.dev.close()


def _open_device(device: str):
    if device.startswith("/dev/hidraw"):
        return _HidrawFile(device)
    return _HidapiDevice(device)
