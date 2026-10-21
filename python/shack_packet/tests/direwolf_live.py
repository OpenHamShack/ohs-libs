"""A live direwolf on audio pipes, for the connected-mode interop tests.

direwolf 1.7 reads audio from UDP (`ADEVICE UDP:port`) but only writes to
an ALSA device. ALSA's user-space `file` plugin is one (alsa.conf defines
`file:PATH`, a null device that copies what is played to PATH), so
direwolf's transmit audio goes to a FIFO this process reads: no sound
card, no kernel module, nothing any other program on the host can see -
the brief rules out snd-aloop and any added audio device. The path must be
short (direwolf keeps 30 characters of the device name), so the FIFO is in
/tmp; it holds no data on disk. The configuration file is passed by its
bare name with direwolf started in its folder, for the same reason: under
tools/test.sh's longer temporary folder direwolf never opened its AGW
port (a path-length limit, by the look of it; a short path always worked).

The engine side is `PipeLink`, a RadioLink: what the engine transmits is
streamed to direwolf's UDP port at the real-time rate, with silence between
transmissions (direwolf's DCD must see the channel go quiet), and what
direwolf transmits is fed to the engine as it arrives, with silence fed
when the FIFO is idle (so our DCD sees the same). direwolf is driven as an
AGW client would (its port 8000 protocol; here a port of our own).
"""

from __future__ import annotations

import os
import random
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from shack_packet.channel import RadioLink

RATE = 48_000
CHUNK_S = 0.02


def free_port(kind=socket.SOCK_STREAM) -> int:
    """A free port below 32768: direwolf refuses higher AGW port numbers
    and falls back to 8000 - which something else on the host may own."""
    rng = random.Random()
    for _ in range(100):
        port = rng.randrange(20_000, 32_000)
        with socket.socket(socket.AF_INET, kind) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


class PipeLink(RadioLink):
    """The engine's radio: UDP to direwolf's input, a FIFO from its output."""

    def __init__(self, udp_port: int, fifo: str):
        self.rate = RATE
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dest = ("127.0.0.1", udp_port)
        self.fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self._tx = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.keyed = False
        self.sent_audio_s = 0.0
        self.heard_audio_s = 0.0
        self._threads = [threading.Thread(target=self._send_loop, daemon=True),
                         threading.Thread(target=self._recv_loop, daemon=True)]

    def start(self):
        for t in self._threads:
            t.start()
        return self

    def close(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        os.close(self.fifo_fd)
        self.udp.close()

    def key(self):
        self.keyed = True

    def unkey(self):
        self.keyed = False

    def send_pcm(self, pcm: bytes):
        with self._lock:
            self._tx += pcm
        self.sent_audio_s += len(pcm) / 2 / RATE

    def _send_loop(self):
        n = int(RATE * CHUNK_S) * 2
        silence = bytes(n)
        due = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                chunk, self._tx = bytes(self._tx[:n]), self._tx[n:]
            chunk = chunk + silence[len(chunk):]
            # direwolf takes s16le mono at ARATE in datagrams.
            self.udp.sendto(chunk, self.dest)
            due += CHUNK_S
            time.sleep(max(0.0, due - time.monotonic()))

    def _recv_loop(self):
        """direwolf writes its transmit audio into the file plugin as fast as
        it can and then holds PTT for the audio's length; fed to the engine
        as it arrives, the engine heard each frame before direwolf had
        finished "transmitting" it and answered into direwolf's own
        transmission, which direwolf (half duplex) never heard. So the audio
        is paced: read into a buffer, fed in real time, silence when the
        buffer is empty - which is also what lets our DCD fall."""
        n = int(RATE * CHUNK_S) * 2
        silence = bytes(n)
        buffered = bytearray()
        due = time.monotonic()
        while not self._stop.is_set():
            while True:
                ready, _, _ = select.select([self.fifo_fd], [], [], 0)
                if not ready:
                    break
                try:
                    data = os.read(self.fifo_fd, 65536)
                except BlockingIOError:
                    break
                if not data:
                    break               # no writer: end of file, for now
                buffered += data
            chunk, buffered[:n] = bytes(buffered[:n]), b""
            if chunk:
                self.heard_audio_s += len(chunk) / 2 / RATE
            self.sink.pcm_in(chunk + silence[len(chunk):])
            due += CHUNK_S
            time.sleep(max(0.0, due - time.monotonic()))


class Direwolf:
    """direwolf with UDP audio in, a FIFO out, and an AGW client."""

    def __init__(self, workdir: Path, mycall: str, extra: str = ""):
        if not shutil.which("direwolf"):
            raise FileNotFoundError("direwolf")
        self.workdir = workdir
        self.mycall = mycall
        self.udp_port = free_port(socket.SOCK_DGRAM)
        self.agw_port = free_port()
        self.fifo = tempfile.mktemp(prefix="dw", dir="/tmp")
        os.mkfifo(self.fifo)
        conf = workdir / "direwolf.conf"
        conf.write_text(f"ADEVICE UDP:{self.udp_port} file:{self.fifo}\nARATE {RATE}\nCHANNEL 0\n"
                        f"MYCALL {mycall}\nMODEM 1200\nAGWPORT {self.agw_port}\nKISSPORT 0\n{extra}\n")
        self.conf = conf
        self.log_path = workdir / "direwolf.log"
        self.proc = None
        self.sock = None
        self.frames: list[tuple[str, bytes, bytes]] = []
        self._buf = b""

    def link(self) -> PipeLink:
        # The reader end opens first, so direwolf's open for writing does
        # not block.
        return PipeLink(self.udp_port, self.fifo)

    def start(self):
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(["stdbuf", "-oL", "direwolf", "-t", "0", "-c", self.conf.name], stdout=self._log,
                                     stderr=subprocess.STDOUT, cwd=self.workdir)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.agw_port), timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            text = self.log()
            self.stop()
            raise RuntimeError("direwolf's AGW port did not open: " + text)
        self.sock.settimeout(0.1)
        return self

    def stop(self):
        if self.sock is not None:
            self.sock.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc is not None:
            self._log.close()
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    # -- AGW -----------------------------------------------------------------

    def send(self, kind: str, call_from: str = "", call_to: str = "", data: bytes = b"", pid: int = 0):
        header = struct.pack("<B3xcxBx10s10sI4x", 0, kind.encode(), pid, call_from.encode().ljust(10, b"\0"),
                             call_to.encode().ljust(10, b"\0"), len(data))
        self.sock.sendall(header + data)

    def register(self, call: str):
        self.send("X", call)

    def poll(self, wait_s: float = 0.0):
        """Read whatever AGW frames have arrived: (kind, from, to, data)."""
        end = time.monotonic() + wait_s
        while True:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
            except socket.timeout:
                pass
            while len(self._buf) >= 36:
                kind = chr(self._buf[4])
                call_from = self._buf[8:18].split(b"\0")[0].decode()
                call_to = self._buf[18:28].split(b"\0")[0].decode()
                (length,) = struct.unpack("<I", self._buf[28:32])
                if len(self._buf) < 36 + length:
                    break
                data = self._buf[36:36 + length]
                self._buf = self._buf[36 + length:]
                self.frames.append((kind, call_from + ">" + call_to, data))
            if time.monotonic() >= end:
                break
        return self.frames

    def wait_for(self, predicate, timeout_s: float):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self.poll(0.1)
            found = [f for f in self.frames if predicate(f)]
            if found:
                return found[0]
        return None
