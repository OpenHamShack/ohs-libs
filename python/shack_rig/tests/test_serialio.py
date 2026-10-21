"""The serial port's own promises, on a pty and on a stand-in pyserial.

What a PTT driver and the Yaesu CAT link rely on, beyond what test_usb.py
already proves about opening and exclusivity: the stop bits asked for are
the ones set, RTS and DTR are low from the moment of opening and lowered
again on close, a line goes up only when `set_line` is asked, `read_wait`
waits and returns, and on a system without termios (Windows) pyserial is
opened with both lines already low - the FTX-1 CAT manual's p. 2 trap,
"the transceiver may switch to the transmit mode when the computer is
started".

A pty has no modem lines, so the line tests watch the ioctls the port
makes: the order of those calls is the behaviour, and it is the part that
keys a radio if it is wrong.
"""

from __future__ import annotations

import os
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from shack_rig import serialio
from shack_rig.serialio import SerialError, SerialPort

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def pty():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    yield master, path
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.fixture
def lines(monkeypatch):
    """Record every modem-line ioctl, and let it succeed as a real UART would."""
    calls: list[tuple[str, int]] = []
    real = serialio.fcntl.ioctl
    names = {termios.TIOCMBIS: "raise", termios.TIOCMBIC: "lower"}

    def ioctl(fd, request, arg=0, *rest):
        if request in names:
            import struct
            calls.append((names[request], struct.unpack("I", arg)[0]))
            return arg
        return real(fd, request, arg, *rest)

    monkeypatch.setattr(serialio.fcntl, "ioctl", ioctl)
    return calls


BOTH = termios.TIOCM_RTS | termios.TIOCM_DTR


def test_two_stop_bits_are_set_when_asked(pty):
    _, path = pty
    for bits, want in ((1, 0), (2, termios.CSTOPB)):
        port = SerialPort(path, 38400, stop_bits=bits)
        port.open()
        try:
            cflag = termios.tcgetattr(port.fileno())[2]
            assert cflag & termios.CSTOPB == want
        finally:
            port.close()


def test_a_stop_bit_count_a_port_cannot_have_is_refused():
    with pytest.raises(SerialError):
        SerialPort("/dev/null", 38400, stop_bits=3)


def test_the_lines_are_low_from_opening_and_raised_only_when_asked(pty, lines):
    _, path = pty
    port = SerialPort(path, 4800)
    port.open()
    # The first and only line operation of an open is lowering both.
    assert lines == [("lower", BOTH)]
    assert port.line_state == {"rts": False, "dtr": False}
    assert port.set_line("rts", True)
    assert lines[-1] == ("raise", termios.TIOCM_RTS)
    assert port.line_state["rts"] and not port.line_state["dtr"]
    assert port.set_line("rts", False)
    assert lines[-1] == ("lower", termios.TIOCM_RTS)
    port.set_line("dtr", True)
    port.close()
    # Closing lowers both, whatever a driver left up.
    assert lines[-1] == ("lower", BOTH)
    assert port.line_state == {"rts": False, "dtr": False}


def test_a_line_on_a_port_without_modem_lines_says_so(pty):
    _, path = pty
    port = SerialPort(path, 4800)
    port.open()
    try:
        # A pty refuses TIOCMBIS: the driver must hear that it did not key.
        assert port.set_line("rts", True) is False
        assert port.line_state["rts"] is False
        with pytest.raises(SerialError):
            port.set_line("cts", True)
    finally:
        port.close()
    assert port.set_line("rts", True) is False            # closed: nothing to set


def test_read_wait_waits_for_bytes_and_gives_up_on_time(pty):
    master, path = pty
    port = SerialPort(path, 38400)
    port.open()
    try:
        t = time.monotonic()
        assert port.read_wait(0.1) == b""
        assert time.monotonic() - t >= 0.09
        os.write(master, b"FA014250000;")
        assert port.read_wait(1.0) == b"FA014250000;"
    finally:
        port.close()


#: A stand-in pyserial, recording what was done to it and in what order.
FAKE_SERIAL = r'''
import sys, types
events = []
class Serial:
    def __init__(self):
        self.__dict__["is_open"] = False
        self.__dict__["_rx"] = b"ID0840;"
    def __setattr__(self, name, value):
        events.append(("set", name, value, self.is_open))
        self.__dict__[name] = value
    def open(self):
        events.append(("open", self.__dict__.get("port")))
        self.__dict__["is_open"] = True
    def close(self):
        events.append(("close",))
        self.__dict__["is_open"] = False
    @property
    def in_waiting(self):
        return len(self._rx)
    def read(self, n):
        out, self.__dict__["_rx"] = self._rx[:n], self._rx[n:]
        return out
    def write(self, data):
        events.append(("write", bytes(data).decode()))
        return len(data)
mod = types.ModuleType("serial")
mod.Serial = Serial
mod.events = events
sys.modules["serial"] = mod
'''

CHILD = FAKE_SERIAL + r'''
sys.modules["fcntl"] = None
sys.modules["termios"] = None
sys.path.insert(0, sys.argv[1])
from shack_rig.serialio import SerialError, SerialPort
port = SerialPort("COM7", 38400, stop_bits=2)
port.open()
assert port.write(b"ID;")
got = port.read_wait(0.5)
port.set_line("rts", True)
try:
    port.fileno()
except SerialError:
    print("NO-FILENO")
port.close()
import json
print("GOT", got.decode())
print("EVENTS", json.dumps(events))
'''


def test_without_termios_pyserial_opens_with_both_lines_already_low():
    out = subprocess.run([sys.executable, "-c", CHILD, str(SRC)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    events = json.loads(out.stdout.split("EVENTS ", 1)[1])
    assert "GOT ID0840;" in out.stdout
    assert "NO-FILENO" in out.stdout
    opened = next(i for i, e in enumerate(events) if e[0] == "open")
    before = {e[1]: e[2] for e in events[:opened] if e[0] == "set"}
    # Both lines were set low, flow control off, before the port was opened.
    assert before["rts"] is False and before["dtr"] is False
    assert before["rtscts"] is False and before["dsrdtr"] is False
    assert before["stopbits"] == 2 and before["baudrate"] == 38400
    assert events[opened] == ["open", "COM7"]
    # The PTT line went up only when asked, after opening...
    raised = [i for i, e in enumerate(events) if e[:3] == ["set", "rts", True]]
    assert raised and raised[0] > opened
    # ...and closing lowered both before the close.
    closed = next(i for i, e in enumerate(events) if e[0] == "close")
    lowered = {e[1] for e in events[raised[0]:closed] if e[0] == "set" and e[2] is False}
    assert lowered == {"rts", "dtr"}
