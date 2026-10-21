"""A pretend TM-V71 on the far end of a pty, answering its PC port as
LA3QMA's command list and hamlib's TM-D710 backend say it does.

    radio = FakeTmV71(); radio.start()   # radio.device is the PC port's path
    ...
    radio.stop()

For tests (this library's and the packet links'), as yaesu/fake.py is for
the FTX-1. Written from the radio's side and from the sources, without
importing cat.py's builders, so a misreading is not silently shared by
both. What it models:

- ID, TY, FV, BC, VM, FO, BY, PC, SQ, MU, DL and MR; anything else is
  answered "?" (CHIRP's iserr: "?" or "N"). Every set is echoed in the
  read's shape, as hamlib's push_fo/push_mu read their answer back.
- Two bands, each on a segment (VHF, UHF, or 1.2 GHz on B), with a VFO and
  a memory channel. An FO whose frequency is off the band's segment is
  answered "N" and changes nothing - hamlib: "no command to change the
  frequency band of a VFO". That the real radio answers N there (rather
  than ?) is this fake's assumption; the library treats both alike.
- The DATA jack: `pks(on)` is the interface pulling PKS low, which keys
  the band EXT. DATA BAND names for transmit. `interface_port(line)` is a
  Digirig-style serial port whose `line` (RTS or DTR) is wired to PKS, and
  `cm108_writer()` a CM108 chip whose GPIO 3 is - so a test drives the
  real PTT code and sees the radio key.
- `TX` and `RX`: LA3QMA and hamlib both list them. The fake keys on TX
  like the radio would, and records it in `cat_keys`, so a test can say
  the library never sent one.

What it cannot be: the radio's timing, its audio (tests route a
FakeBackend sound device), or a front panel.
"""

from __future__ import annotations

import os
import threading
import time

#: Fields 29-34 (the PF keys) are hex, two digits (hamlib push_mu "%02X");
#: the rest decimal, P9 two digits. Values are an ordinary radio's: menu 517
#: EXT. DATA BAND on B (factory, RC-D710 p. 24), 518 at 1200, 520 SQC BUSY.
FACTORY_MENU = ("1,3,0,1,0,4,1,0,10,0,1,1,1,0,0,2,0,0,0,0,0,0,1,0,0,8,0,0,"
                "04,05,00,06,0A,0B,0,0,0,1,0,1,1,1").split(",")

SEGMENTS = {"vhf": (118_000_000, 174_000_000), "uhf": (300_000_000, 524_000_000),
            "1200": (800_000_000, 1_300_000_000)}


def _segment(hz: int) -> str | None:
    for name, (lo, hi) in SEGMENTS.items():
        if lo <= hz <= hi:
            return name
    return None


class FakeTmV71:
    def __init__(self, freq_a: int = 145_500_000, freq_b: int = 433_500_000, answer: bool = True,
                 type_code: str = "M"):
        # FO's thirteen fields per band, as strings in hamlib's widths. A
        # repeater-ish band A (minus shift, tone on) and a plain band B.
        self.vfo = {
            0: ["0", f"{freq_a:010d}", "0", "2", "0", "1", "0", "0", "08", "08", "000", "00600000", "0"],
            1: ["1", f"{freq_b:010d}", "0", "0", "0", "0", "0", "0", "08", "08", "000", "05000000", "0"],
        }
        #: VM per band: 0 VFO, 1 memory. Band A starts on a memory channel.
        self.band_mode = {0: 1, 1: 0}
        self.memory = {0: 12, 1: 0}
        self.menu = list(FACTORY_MENU)
        self.power = {0: 0, 1: 0}              #: PC: 0 H, 1 M, 2 L
        self.squelch = {0: 0x05, 1: 0x05}
        self.squelch_open = {0: False, 1: False}
        self.ctrl_band, self.ptt_band = 0, 0
        self.dual = 0
        self.type_code = type_code
        self.answer = answer
        self.commands: list[str] = []
        self.refused: list[str] = []
        self.menu_writes: list[list[str]] = []
        #: Every TX/RX the PC port was sent: the library must never send TX.
        self.cat_keys: list[str] = []
        #: (monotonic, band or None) each time the transmitter changes.
        self.tx_history: list[tuple[float, int | None]] = []
        self.tx_band: int | None = None
        self.pks_low = False
        self._master, self._slave = os.openpty()
        self.device = os.ttyname(self._slave)
        self._buf = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-tmv71", daemon=True)

    def start(self) -> FakeTmV71:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- what a test asks and does ---------------------------------------------------

    def freq(self, band: int) -> int:
        return int(self.vfo[band][1])

    def data_band(self) -> int:
        return int(self.menu[37])

    def data_speed(self) -> int:
        return 9600 if self.menu[38] == "1" else 1200

    def tx_side(self) -> int:
        """The band the DATA jack transmits on (EXT. DATA BAND, menu 517)."""
        return {0: 0, 1: 1, 2: 0, 3: 1}[self.data_band()]

    def rx_side(self) -> int:
        return {0: 0, 1: 1, 2: 1, 3: 0}[self.data_band()]

    @property
    def transmitting(self) -> bool:
        return self.tx_band is not None

    def _key(self, band: int | None) -> None:
        if band != self.tx_band:
            self.tx_band = band
            self.tx_history.append((time.monotonic(), band))

    def pks(self, low: bool) -> None:
        """The DATA jack's PKS pulled low (keyed) or released."""
        self.pks_low = bool(low)
        self._key(self.tx_side() if low else None)

    def set_busy(self, band: int, open_: bool) -> None:
        self.squelch_open[band] = bool(open_)

    def interface_port(self, line: str = "rts") -> FakeInterfacePort:
        return FakeInterfacePort(self, line)

    def cm108_writer(self, gpio: int = 3) -> FakeCm108:
        return FakeCm108(self, gpio)

    # -- the PC port ----------------------------------------------------------------------

    def _run(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._master], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(self._master, 1024)
            except OSError:
                return
            self._buf += data.decode("ascii", "replace")
            while "\r" in self._buf:
                line, self._buf = self._buf.split("\r", 1)
                line = line.strip()
                if line:
                    self._handle(line)

    def _say(self, text: str) -> None:
        if self.answer:
            try:
                os.write(self._master, (text + "\r").encode("ascii"))
            except OSError:
                pass

    def _no(self, cmd: str, why: str = "?") -> None:
        self.refused.append(cmd)
        self._say(why)

    def _handle(self, line: str) -> None:
        self.commands.append(line)
        name, _, rest = line.partition(" ")
        p = [x.strip() for x in rest.split(",")] if rest else []
        band = int(p[0]) if p and p[0] in ("0", "1") else None
        if name == "ID" and not p:
            self._say("ID TM-V71")
        elif name == "TY" and not p:
            self._say(f"TY {self.type_code},0,0,1,0")
        elif name == "FV" and p == ["0"]:
            self._say("FV 0,1.00,2.10,A,1")
        elif name == "BC" and not p:
            self._say(f"BC {self.ctrl_band},{self.ptt_band}")
        elif name == "DL" and not p:
            self._say(f"DL {self.dual}")
        elif name == "VM" and len(p) == 1 and band is not None:
            self._say(f"VM {band},{self.band_mode[band]}")
        elif name == "VM" and len(p) == 2 and band is not None and p[1] in "0123":
            self.band_mode[band] = int(p[1])
            self._say(f"VM {band},{self.band_mode[band]}")
        elif name == "MR" and len(p) == 1 and band is not None:
            if self.band_mode[band] != 1:
                self._no(line, "N")
            else:
                self._say(f"MR {band},{self.memory[band]:03d}")
        elif name == "FO" and len(p) == 1 and band is not None:
            self._say("FO " + ",".join(self.vfo[band]))
        elif name == "FO" and len(p) == 13 and band is not None:
            self._set_vfo(line, band, p)
        elif name == "BY" and len(p) == 1 and band is not None:
            self._say(f"BY {band},{1 if self.squelch_open[band] else 0}")
        elif name == "PC" and len(p) == 1 and band is not None:
            self._say(f"PC {band},{self.power[band]}")
        elif name == "PC" and len(p) == 2 and band is not None and p[1] in ("0", "1", "2"):
            self.power[band] = int(p[1])
            self._say(f"PC {band},{self.power[band]}")
        elif name == "SQ" and len(p) == 1 and band is not None:
            self._say(f"SQ {band},{self.squelch[band]:02X}")
        elif name == "MU" and not p:
            self._say("MU " + ",".join(self.menu))
        elif name == "MU" and len(p) == len(FACTORY_MENU):
            self._set_menu(line, p)
        elif name == "TX" and not p:
            self.cat_keys.append("TX")
            self._key(self.ptt_band)
            self._say("TX 0")
        elif name == "RX" and not p:
            self.cat_keys.append("RX")
            if not self.pks_low:
                self._key(None)
            self._say("RX")
        else:
            self._no(line)

    def _set_vfo(self, line: str, band: int, p: list[str]) -> None:
        widths = (1, 10, 1, 1, 1, 1, 1, 1, 2, 2, 3, 8, 1)
        if any(len(x) != w for x, w in zip(p, widths)) or not p[1].isdigit():
            self._no(line)
            return
        want = int(p[1])
        if _segment(want) != _segment(self.freq(band)) or (band == 0 and _segment(want) == "1200"):
            self._no(line, "N")
            return
        if p[12] not in ("0", "1", "2"):
            self._no(line, "N")
            return
        self.vfo[band] = list(p)
        self._say("FO " + ",".join(self.vfo[band]))

    def _set_menu(self, line: str, p: list[str]) -> None:
        if any(len(x) != len(y) for x, y in zip(p, self.menu)):
            self._no(line)
            return
        if p[37] not in ("0", "1", "2", "3") or p[38] not in ("0", "1") or p[39] not in "012345":
            self._no(line, "N")
            return
        self.menu = list(p)
        self.menu_writes.append(list(p))
        self._say("MU " + ",".join(self.menu))


class FakeInterfacePort:
    """A sound-card interface's serial port (a Digirig's CP2102) with one
    line wired to the DATA jack's PKS, as serialio.SerialPort looks to a
    PTT driver: open, set_line, close. Lines start low; the wired one, high,
    pulls PKS low (keys the radio)."""

    def __init__(self, radio: FakeTmV71, line: str = "rts", device: str = "fake-interface-port"):
        self.radio = radio
        self.wired = line
        self.device = device
        self.opened = False
        self.closed = False
        self.line_state = {"rts": False, "dtr": False}
        self.history: list[tuple[str, bool]] = []

    def open(self) -> None:
        self.opened = True

    def set_line(self, name: str, on: bool) -> bool:
        if not self.opened or self.closed:
            return False
        self.line_state[name] = bool(on)
        self.history.append((name, bool(on)))
        if name == self.wired:
            self.radio.pks(bool(on))
        return True

    def close(self) -> None:
        for name in ("rts", "dtr"):
            if self.line_state[name]:
                self.set_line(name, False)
        self.closed = True

    def write(self, data: bytes) -> bool:
        return True

    def read_wait(self, timeout: float) -> bytes:
        time.sleep(timeout)
        return b""


class FakeCm108:
    """A CM108 chip's HID device with GPIO `gpio` wired to PKS: decodes
    each 5-byte report as Dire Wolf's cm108_write builds it."""

    def __init__(self, radio: FakeTmV71, gpio: int = 3):
        self.radio = radio
        self.gpio = gpio
        self.reports: list[bytes] = []
        self.closed = False

    def __call__(self, device: str) -> FakeCm108:
        """As cm108.Cm108Ptt's `writer`: the device to write to."""
        self.device = device
        return self

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        if len(data) != 5 or data[0] != 0:
            raise OSError(f"not a CM108 GPIO report: {data!r}")
        self.reports.append(bytes(data))
        bit = 1 << (self.gpio - 1)
        if data[3] & bit:
            self.radio.pks(bool(data[2] & bit))

    def close(self) -> None:
        self.closed = True
