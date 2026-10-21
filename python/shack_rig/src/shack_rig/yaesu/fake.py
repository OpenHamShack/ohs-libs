"""A pretend FTX-1 on the far end of a pty, answering CAT as the manual says.

    radio = FakeFtx1(); radio.start()    # radio.device is the CAT-1 port's path
    ...
    radio.stop()

For tests (this library's and the packet links'), as icom/fake.py is for
the LAN. Written from the FTX-1 CAT manual's tables, from the radio's side,
without importing cat.py's builders, so a misreading is not silently shared
by both. What it models: FA, MD, TX, RI, SM, CT, OS, AI, ID, IF and the EX
items the library touches (MOD SOURCE and RPTT SELECT for the FM and DATA
blocks), refusing anything else with `?;`; a squelch a test opens; and the
Standard COM port's modem lines (`line_port()`), which key the radio only
when RPTT SELECT for the current mode names that line - the manual's p. 2
trap, so a test can show a line raised at the wrong moment would key it.

What it cannot be: the radio's timing (it answers at once), its USB codec
(tests route a FakeBackend sound device through `transmitting_usb`), or
whether DATA-FM really reads the DATA block's MOD SOURCE - here it does, and
the library sets both blocks so that either answer works.
"""

from __future__ import annotations

import os
import threading
import time

#: Mode codes in the DATA block of the menu (EX 01 04) rather than the FM one.
DATA_CODES = set("8ACF")
FM_CODES = set("4ABF")


class FakeFtx1:
    def __init__(self, freq: int = 145_500_000, answer: bool = True):
        self.freq = freq
        self.mode = "4"                     #: MD code: FM
        self.tx_state = 0                   #: 0 RX, 1 CAT TX, 2 keyed by the radio (a line)
        self.squelch_open = False
        self.smeter = 40
        self.ctcss = 2                      #: CT P2: 2 CTCSS enc/dec, as a repeater channel has it
        self.shift = 1                      #: OS P2: 1 plus shift
        self.auto_info = 0
        self.answer = answer
        #: EX items: (p1, p2, p3) -> [value, digits]. MOD SOURCE ships MIC (0);
        #: RPTT SELECT ships OFF (0).
        self.menu = {(1, 4, 13): [0, 1], (1, 3, 12): [0, 1], (1, 4, 15): [0, 1], (1, 3, 14): [0, 1],
                     # USB MOD GAIN, DATA and FM blocks: 050 as a guess at
                     # the factory value (the manual gives none).
                     (1, 4, 14): [50, 3], (1, 3, 13): [50, 3]}
        #: The data set-up's switches (cat.py), as an operator might have
        #: left them: DNR on at 5, the rest off. Set and read as the manual
        #: says (BC0 n; RL0 nn; NL0 nnn; VX n; ML0 nnn; PR0 n with 1 = OFF).
        self.switches = {"BC": 0, "RL": 5, "NL": 0, "VX": 0, "ML": 0, "PR": 1}
        self.menu_writes: list[tuple[tuple[int, int, int], int]] = []
        self.commands: list[str] = []
        self.refused: list[str] = []
        self.ptt_times: list[tuple[float, int]] = []
        #: How long the pretend transmitter takes to reach power after a key,
        #: and to fall after an unkey, as RM5 (the PO meter) shows them: for
        #: a turnaround measurement to find.
        self.rf_rise_s = 0.0
        self.rf_fall_s = 0.0
        self.rf_power = 180
        self.lines = {"rts": False, "dtr": False}
        self.line_history: list[tuple[float, str, bool]] = []
        self._master, self._slave = os.openpty()
        self.device = os.ttyname(self._slave)
        self._buf = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-ftx1", daemon=True)

    def start(self) -> FakeFtx1:
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

    # -- what a test asks ------------------------------------------------------

    @property
    def transmitting(self) -> bool:
        return self.tx_state != 0

    def mod_source(self) -> int:
        """MOD SOURCE as the current mode reads it (module docstring)."""
        block = (1, 4, 13) if self.mode in DATA_CODES else (1, 3, 12)
        return self.menu[block][0]

    def transmitting_usb(self) -> bool:
        """Keyed and taking its modulation from the USB codec: what a sound
        route from the computer to "the air" is gated on."""
        return self.transmitting and self.mod_source() == 1

    def power_now(self) -> int:
        """The PO meter now: up rf_rise_s after the last key, down
        rf_fall_s after the last unkey."""
        if not self.ptt_times:
            return 0
        t, state = self.ptt_times[-1]
        age = time.monotonic() - t
        if state:
            return self.rf_power if age >= self.rf_rise_s else 0
        return self.rf_power if age < self.rf_fall_s else 0

    def rptt_select(self) -> int:
        block = (1, 4, 15) if self.mode in DATA_CODES else (1, 3, 14)
        return self.menu[block][0]

    def line_port(self) -> FakeLinePort:
        return FakeLinePort(self)

    # -- the modem lines, from FakeLinePort -----------------------------------------

    def line(self, name: str, on: bool) -> None:
        self.lines[name] = on
        self.line_history.append((time.monotonic(), name, on))
        wanted = {1: "rts", 2: "dtr"}.get(self.rptt_select())
        if wanted == name:
            self._key(2 if on else 0)

    def _key(self, state: int) -> None:
        if state != self.tx_state:
            self.tx_state = state
            self.ptt_times.append((time.monotonic(), state))

    # -- CAT -----------------------------------------------------------------------

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
            while ";" in self._buf:
                cmd, self._buf = self._buf.split(";", 1)
                self._handle(cmd.strip().upper())

    def _say(self, text: str) -> None:
        if self.answer:
            try:
                os.write(self._master, text.encode("ascii"))
            except OSError:
                pass

    def _no(self, cmd: str) -> None:
        self.refused.append(cmd)
        self._say("?;")

    def _if(self) -> str:
        return (f"IF00000{self.freq:09d}+000000{self.mode}0{self.ctcss if self.ctcss < 6 else 0}00"
                f"{self.shift if self.shift < 3 else 0};")

    def _handle(self, cmd: str) -> None:
        self.commands.append(cmd)
        c, p = cmd[:2], cmd[2:]
        if c == "ID" and not p:
            self._say("ID0840;")
        elif c == "FA" and not p:
            self._say(f"FA{self.freq:09d};")
        elif c == "FA" and len(p) == 9 and p.isdigit() and 30_000 <= int(p) <= 470_000_000:
            self.freq = int(p)
        elif c == "MD" and p == "0":
            self._say(f"MD0{self.mode};")
        elif c == "MD" and len(p) == 2 and p[0] == "0" and p[1] in "123456789ABCDEFHI":
            self.mode = p[1]
        elif c == "TX" and not p:
            self._say(f"TX{self.tx_state};")
        elif c == "TX" and p in ("0", "1"):
            self._key(int(p))
        elif c == "RI" and p == "0":
            tx = 1 if self.tx_state else 0
            self._say(f"RI000{tx}000{1 if self.squelch_open else 0};")
        elif c == "RM" and p == "5":
            self._say(f"RM5{self.power_now():03d}000;")
        elif c == "SM" and p == "0":
            self._say(f"SM0{self.smeter:03d};")
        elif c == "CT" and p == "0":
            self._say(f"CT0{self.ctcss};")
        elif c == "CT" and len(p) == 2 and p[0] == "0" and p[1] in "012345":
            self.ctcss = int(p[1])
        elif c == "OS" and p == "0":
            self._say(f"OS0{self.shift};")
        elif c == "OS" and len(p) == 2 and p[0] == "0" and p[1] in "0123":
            if self.mode not in FM_CODES:
                self._no(cmd)               # "This command can be activated only with an FM mode."
            else:
                self.shift = int(p[1])
        elif c in ("BC", "RL", "NL", "ML", "PR") and p == "0":
            width = {"BC": 1, "RL": 2, "NL": 3, "ML": 3, "PR": 1}[c]
            self._say(f"{c}0{self.switches[c]:0{width}d};")
        elif c in ("BC", "RL", "NL", "ML", "PR") and p[:1] == "0" and p[1:].isdigit():
            limits = {"BC": (1, 0, 1), "RL": (2, 0, 10), "NL": (3, 0, 10), "ML": (3, 0, 1), "PR": (1, 1, 2)}
            width, lo, hi = limits[c]
            if len(p) - 1 != width or not lo <= int(p[1:]) <= hi:
                self._no(cmd)
            else:
                self.switches[c] = int(p[1:])
        elif c == "VX" and not p:
            self._say(f"VX{self.switches['VX']};")
        elif c == "VX" and p in ("0", "1"):
            self.switches["VX"] = int(p)
        elif c == "AI" and not p:
            self._say(f"AI{self.auto_info};")
        elif c == "AI" and p in ("0", "1"):
            self.auto_info = int(p)
        elif c == "IF" and not p:
            self._say(self._if())
        elif c == "EX" and len(p) >= 6 and p[:6].isdigit():
            where = (int(p[0:2]), int(p[2:4]), int(p[4:6]))
            item = self.menu.get(where)
            if item is None:
                self._no(cmd)
            elif len(p) == 6:
                self._say(f"EX{p[:6]}{item[0]:0{item[1]}d};")
            elif len(p) == 6 + item[1] and p[6:].isdigit():
                item[0] = int(p[6:])
                self.menu_writes.append((where, item[0]))
            else:
                self._no(cmd)
        else:
            self._no(cmd)


class FakeLinePort:
    """The Standard COM port of a FakeFtx1, as serialio.SerialPort looks to a
    PTT driver: open, set_line, close - with every line change recorded and
    handed to the radio. Lines start low; `open()` here, as serialio's,
    never raises one."""

    def __init__(self, radio: FakeFtx1, device: str = "fake-standard-port"):
        self.radio = radio
        self.device = device
        self.opened = False
        self.closed = False
        self.line_state = {"rts": False, "dtr": False}

    def open(self) -> None:
        self.opened = True

    def set_line(self, name: str, on: bool) -> bool:
        if not self.opened or self.closed:
            return False
        self.line_state[name] = bool(on)
        self.radio.line(name, bool(on))
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
