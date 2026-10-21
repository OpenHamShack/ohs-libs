"""An rtl_tcp client: an RTL-SDR on another computer, over the network.

rtl_tcp (osmocom rtl-sdr's rtl_tcp.c, and every fork and SDR server that
copies it: rtl-sdr-blog, SDR# and SDR++'s servers, spyserver's rtl_tcp
mode) is a small protocol. The server sends a 12-byte header - "RTL0",
the tuner type and the number of gain steps, both 32-bit big-endian - and
then nothing but samples, unsigned 8-bit I and Q, for as long as the
connection lasts. The client sends commands of five bytes: one for the
command, four for its parameter, big-endian. There are no replies, so a
setting cannot be confirmed; what was sent is in `status()`.

Pure Python, a socket and numpy: nothing for an installer to bundle.

Choices:

- **The crystal correction is not sent** (command 5 stays at its default):
  the channeliser corrects in software for every source, to a fraction of
  a ppm, and a server may share its dongle with other clients that do not
  want our correction.
- **Gain**: None asks for the tuner's automatic gain (mode 0); a number is
  manual gain in dB, sent in tenths. The server picks its nearest step.
- **Nothing that powers anything**: bias-tee (command 14) is never sent.
  The server's own setting stands.
- **Timeouts**: connecting waits `timeout_s`; a read that sees nothing for
  `timeout_s` is a dead server (`read-failed`), and a closed connection
  is `disconnected`. Neither reconnects by itself: the app decides, and
  says so.
- **Losses**: TCP loses nothing, but rtl_tcp.c drops samples itself when
  its client falls behind (its "ll+" queue limit), unseen at this end; the
  wall-clock check in IqSource counts those (LAG_LIMIT_S).
"""

from __future__ import annotations

import socket
import struct

import numpy as np

from .source import IqSource, SdrError, cu8_to_complex

CMD_FREQ, CMD_RATE, CMD_GAIN_MODE, CMD_GAIN, CMD_PPM, CMD_AGC = 1, 2, 3, 4, 5, 8
#: rtl_tcp's tuner types, for status().
TUNERS = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}


class RtlTcpSource(IqSource):
    kind = "rtl_tcp"

    def __init__(self, host: str, port: int = 1234, centre: float = 145_312_500.0, rate: float = 2_400_000,
                 gain: float | None = None, *, timeout_s: float = 5.0, block_s: float = 0.05, rtl_agc: bool = False):
        super().__init__(centre, rate, gain)
        self.host, self.port = host, int(port)
        self.timeout_s = timeout_s
        self.rtl_agc = rtl_agc
        self.block_bytes = int(rate * block_s) * 2
        self.sock: socket.socket | None = None
        self.tuner = ""
        self.gain_steps = 0
        self.sent: list[tuple[int, int]] = []
        self._carry = b""

    def _open(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as e:
            raise SdrError("no-device", f"rtl_tcp at {self.host}:{self.port}: {e}") from e
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
            head = self._exactly(12)
        except SdrError:
            self._close()
            raise
        if head[:4] != b"RTL0":
            self._close()
            raise SdrError("not-receiver", f"{self.host}:{self.port} did not answer as rtl_tcp ({head[:4]!r})")
        tuner, self.gain_steps = struct.unpack(">II", head[4:12])
        self.tuner = TUNERS.get(tuner, str(tuner))
        self._carry = head[12:]
        self._command(CMD_RATE, int(self.rate))
        self._command(CMD_FREQ, int(round(self.centre)))
        self._apply_gain()
        self._command(CMD_AGC, 1 if self.rtl_agc else 0)

    def _apply_gain(self) -> None:
        if self.gain is None:
            self._command(CMD_GAIN_MODE, 0)
        else:
            self._command(CMD_GAIN_MODE, 1)
            self._command(CMD_GAIN, int(round(self.gain * 10)) & 0xFFFFFFFF)

    def _command(self, cmd: int, value: int) -> None:
        try:
            self.sock.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))
        except OSError as e:
            raise SdrError("disconnected", f"rtl_tcp at {self.host}:{self.port}: {e}") from e
        self.sent.append((cmd, value))

    def set_centre(self, hz: float) -> None:
        """Retune while running. The channels were placed for the old centre:
        the app makes a new Pipeline for the new one (ChannelPlan.centre_for)."""
        self.centre = float(hz)
        if self.sock is not None:
            self._command(CMD_FREQ, int(round(hz)))

    def set_gain(self, gain: float | None) -> None:
        self.gain = gain
        if self.sock is not None:
            self._apply_gain()

    def _exactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            try:
                got = self.sock.recv(n - len(out))
            except socket.timeout as e:
                raise SdrError("read-failed", f"rtl_tcp at {self.host}:{self.port} sent nothing for "
                               f"{self.timeout_s:g} s") from e
            except OSError as e:
                raise SdrError("disconnected", f"rtl_tcp at {self.host}:{self.port}: {e}") from e
            if not got:
                raise SdrError("disconnected", f"rtl_tcp at {self.host}:{self.port} closed the connection")
            out += got
        return out

    def _read(self) -> np.ndarray:
        want = self.block_bytes - len(self._carry)
        raw = self._carry + (self._exactly(want) if want > 0 else b"")
        self._carry = b""
        return cu8_to_complex(raw)

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def status(self) -> dict:
        st = super().status()
        st.update({"host": self.host, "port": self.port, "tuner": self.tuner, "gainSteps": self.gain_steps})
        return st
