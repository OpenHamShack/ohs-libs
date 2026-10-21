"""The library on a system with no termios or fcntl (Windows).

OpenHamShack Icom's desktop form runs on Windows and reaches its radio over
the network; serialio imported both at the top and took the whole package
down there. Run in a child interpreter with both imports blocked, so this
machine's own termios cannot answer for them.
"""

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

CHILD = r"""
import sys
sys.modules["fcntl"] = None
sys.modules["termios"] = None
sys.modules["serial"] = None          # and no pyserial: the refusal, in words
sys.path.insert(0, sys.argv[1])
from shack_rig.icom import UsbLink, lan            # the whole Icom package imports
from shack_rig.serialio import BAUDS, SerialError, SerialPort
port = SerialPort("COM3", 19200)
try:
    port.open()
except SerialError as e:
    print("REFUSED", e)
else:
    print("OPENED")
print("BAUDS", sorted(BAUDS) == [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200])
"""


def test_imports_and_refuses_a_serial_port_without_termios():
    out = subprocess.run([sys.executable, "-c", CHILD, str(SRC)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "REFUSED" in out.stdout and "network" in out.stdout, out.stdout
    assert "BAUDS True" in out.stdout
