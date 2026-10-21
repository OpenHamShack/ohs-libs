"""A made-up 2 m band, for tests, benches and running the app with no SDR.

`SyntheticSource` is an IqSource that generates what an SDR on 2 m would
hear: packet stations on several channels (our own AFSK modulator, FM at
+-3 kHz deviation), each at its own level and frequency error, keyed only
while they send; an FM signal that never stops (a strong neighbour, voice
or a carrier); white noise; and the receiver's own crystal error, applied
as a real receiver suffers it. `truth` lists every frame sent, when and
where, so a test can say exactly what should have been decoded.

`RtlTcpServer` serves the same band over the rtl_tcp protocol on a local
port, so the rtl_tcp client is tested against a server that speaks the
protocol, and the desktop app can be run against it during development
(`python3 -m shack_packet.sdr.synth --port 1234`).

How the crystal error is modelled: an RTL-SDR's tuner and its ADC run from
one crystal, so a receiver whose crystal is e ppm fast has its LO at
fc (1 + e) and its sample clock fast by the same factor. A signal at f on
the air then sits at f / (1 + e) - fc in the samples - exactly what
channeliser.offset_of undoes. (The sample clock's error on the 1200 baud
tones is 50 ppm at worst, which the receiver's bit clock never notices.)

Generation is block by block with every phase carried, so a source can run
for hours (the fake server does) without holding more than a block.
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .. import afsk
from .source import IqSource, complex_to_cu8

AUDIO_RATE = 48_000


@dataclass
class Station:
    """A packet station on `hz`: sends `frames`, one transmission each,
    from `start_s`, every `every_s`. `level_db` is its carrier in dBFS;
    `offset_hz` its own transmitter's error; `preemphasis` sends through a
    mic input's 6 dB an octave."""
    hz: float
    frames: list = field(default_factory=list)
    level_db: float = -30.0
    start_s: float = 0.2
    every_s: float = 1.5
    offset_hz: float = 0.0
    deviation_hz: float = 3_000.0
    fx25: object = None
    txdelay_ms: float = 200.0
    preemphasis: bool = False


@dataclass
class Neighbour:
    """A signal that never stops: `kind` "voice" (FM with three tones that
    wander, like speech's spread across 300-3000 Hz) or "carrier"."""
    hz: float
    level_db: float = -10.0
    kind: str = "voice"
    deviation_hz: float = 3_000.0


@dataclass
class Sent:
    time_s: float      # from the source's first sample
    hz: float
    frame: bytes


class _Burst:
    def __init__(self, start: int, audio: np.ndarray, station: Station):
        self.start = start                      # IQ sample of the first audio sample
        self.audio = audio                      # at AUDIO_RATE, peak 1.0
        self.station = station
        self.phase = 0.0


def _preemphasise(a: np.ndarray, tau_s: float = 750e-6) -> np.ndarray:
    y = a + tau_s * AUDIO_RATE * np.diff(a, prepend=a[:1])
    return y / max(1e-9, np.abs(y).max())


class SyntheticSource(IqSource):
    kind = "synthetic"
    live = False

    def __init__(self, centre: float, rate: float = 2_400_000, stations=(), neighbours=(), *,
                 noise_dbfs: float = -40.0, ppm: float = 0.0, seconds: float | None = 10.0,
                 block_s: float = 0.05, seed: int = 1, drops=(), gain: float | None = None):
        super().__init__(centre, rate, gain)
        self.stations = list(stations)
        self.neighbours = list(neighbours)
        self.noise_dbfs = noise_dbfs
        self.ppm = float(ppm)
        self.seconds = seconds
        self.block = int(round(rate * block_s))
        self.seed = seed
        #: [(at_s, seconds)]: spans the source skips and reports as lost.
        self.drops = sorted(drops)
        self.truth: list[Sent] = []

    def baseband_hz(self, hz: float) -> float:
        return hz / (1.0 + self.ppm * 1e-6) - self.centre

    def _open(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        self.pos = 0
        self.truth = []
        bursts = []
        for st in self.stations:
            for i, frame in enumerate(st.frames):
                t = st.start_s + i * st.every_s
                audio = afsk.modulate([frame], AUDIO_RATE, amplitude=1.0, fx25_mode=st.fx25,
                                      txdelay_ms=st.txdelay_ms, tail_ms=20)
                if st.preemphasis:
                    audio = _preemphasise(audio)
                bursts.append(_Burst(int(t * self.rate), audio, st))
                self.truth.append(Sent(t + st.txdelay_ms / 1000.0, st.hz, bytes(frame)))
        self.bursts = sorted(bursts, key=lambda b: b.start)
        self._nphase = [0.0] * len(self.neighbours)
        self._drops = [(int(a * self.rate), int(s * self.rate)) for a, s in self.drops]

    def _read(self) -> np.ndarray | None:
        total = None if self.seconds is None else int(self.seconds * self.rate)
        while self._drops and self.pos >= self._drops[0][0]:
            _, n = self._drops.pop(0)
            self._generate(self.pos, n)        # the band carries on while nobody listens
            self.pos += n
            self.lose(n)
        if total is not None and self.pos >= total:
            return None
        n = self.block if total is None else min(self.block, total - self.pos)
        if self._drops:
            n = min(n, self._drops[0][0] - self.pos)
        z = self._generate(self.pos, n)
        self.pos += n
        return z

    def _generate(self, i0: int, n: int) -> np.ndarray:
        sigma = math.sqrt(10 ** (self.noise_dbfs / 10) / 2)
        z = (self.rng.standard_normal(n) * sigma + 1j * self.rng.standard_normal(n) * sigma).astype(np.complex128)
        t = (i0 + np.arange(n)) / self.rate
        for k, nb in enumerate(self.neighbours):
            f0 = self.baseband_hz(nb.hz)
            if nb.kind == "carrier":
                inst = np.full(n, f0)
            else:
                a = (0.45 * np.sin(2 * np.pi * (420 + 60 * np.sin(2 * np.pi * 0.7 * t)) * t)
                     + 0.35 * np.sin(2 * np.pi * 1130 * t + 1.0) + 0.2 * np.sin(2 * np.pi * 2650 * t + 2.0))
                inst = f0 + nb.deviation_hz * a
            ph = self._nphase[k] + 2 * np.pi * np.cumsum(inst) / self.rate
            self._nphase[k] = float(ph[-1] % (2 * np.pi))
            z += 10 ** (nb.level_db / 20) * np.exp(1j * ph)
        for b in self.bursts:
            length = int(len(b.audio) * self.rate / AUDIO_RATE)
            lo, hi = max(i0, b.start), min(i0 + n, b.start + length)
            if lo >= hi:
                continue
            ta = (np.arange(lo, hi) - b.start) * (AUDIO_RATE / self.rate)
            a = np.interp(ta, np.arange(len(b.audio)), b.audio)
            st = b.station
            inst = self.baseband_hz(st.hz) + st.offset_hz + st.deviation_hz * a
            ph = b.phase + 2 * np.pi * np.cumsum(inst) / self.rate
            b.phase = float(ph[-1] % (2 * np.pi))
            z[lo - i0:hi - i0] += 10 ** (st.level_db / 20) * np.exp(1j * ph)
        return z.astype(np.complex64)


# -- rtl_tcp ------------------------------------------------------------------

#: rtl_tcp's commands (rtl_tcp.c, osmocom rtl-sdr): one byte, then a
#: 32-bit big-endian parameter.
CMD_NAMES = {1: "freq", 2: "rate", 3: "gain_mode", 4: "gain", 5: "ppm", 6: "if_gain", 7: "test_mode",
             8: "agc", 9: "direct_sampling", 10: "offset_tuning", 11: "rtl_xtal", 12: "tuner_xtal",
             13: "gain_index", 14: "bias_tee"}
TUNER_R820T = 5
R820T_GAINS = (0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372,
               386, 402, 421, 434, 439, 445, 480, 496)


class RtlTcpServer:
    """An rtl_tcp server on 127.0.0.1 serving a SyntheticSource as cu8,
    paced to real time (`realtime=False`: as fast as the client reads).
    `commands` lists every (name, value) received. `cut_after_s` closes the
    connection after that much IQ, as a server that goes away would."""

    def __init__(self, make_source, port: int = 0, *, realtime: bool = True, cut_after_s: float | None = None,
                 header: bytes | None = None):
        self.make_source = make_source            # (centre, rate) -> IqSource
        self.realtime = realtime
        self.cut_after_s = cut_after_s
        self.header = header
        self.commands: list[tuple[str, int]] = []
        self.centre, self.rate = 100e6, 2_048_000
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.alive = True
        self.connections = 0
        self._changed = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="fake-rtl_tcp", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _serve(self) -> None:
        # accept() with a timeout: closing a listening socket does not wake
        # an accept() blocked on it (Linux), so close() would wait it out.
        self.sock.settimeout(0.2)
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            self.connections += 1
            try:
                self._session(conn)
            except OSError:
                pass
            finally:
                # shutdown first: the command reader is blocked in recv on
                # this socket, and close() alone would leave the client
                # waiting on a connection nobody will ever write to.
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()

    def _commands(self, conn: socket.socket, stop: threading.Event) -> None:
        buf = b""
        while not stop.is_set():
            try:
                got = conn.recv(64)
            except OSError:
                break
            if not got:
                break
            buf += got
            while len(buf) >= 5:
                cmd, value = struct.unpack(">BI", buf[:5])
                buf = buf[5:]
                name = CMD_NAMES.get(cmd, f"cmd{cmd}")
                self.commands.append((name, value))
                if name == "freq":
                    self.centre = float(value)
                    self._changed.set()
                elif name == "rate":
                    self.rate = float(value)
                    self._changed.set()
        stop.set()

    def _session(self, conn: socket.socket) -> None:
        conn.sendall(self.header if self.header is not None
                     else b"RTL0" + struct.pack(">II", TUNER_R820T, len(R820T_GAINS)))
        stop = threading.Event()
        threading.Thread(target=self._commands, args=(conn, stop), daemon=True).start()
        # A client sets frequency and rate straight after connecting; give
        # it a moment so the band it hears is the one it asked for.
        self._changed.wait(0.5)
        time.sleep(0.05)
        self._changed.clear()
        src = self.make_source(self.centre, self.rate).open()
        sent = 0
        t0 = time.monotonic()
        while self.alive and not stop.is_set():
            if self._changed.is_set():
                self._changed.clear()
                src = self.make_source(self.centre, self.rate).open()
            block = src.read()
            if block is None:
                break
            data = complex_to_cu8(block.samples)
            conn.sendall(data)
            sent += len(block.samples)
            if self.cut_after_s is not None and sent >= self.cut_after_s * self.rate:
                break
            if self.realtime:
                ahead = sent / self.rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        stop.set()


def demo_band(centre: float, rate: float, seconds: float | None = None) -> SyntheticSource:
    """The default plan's channels with a little traffic on each, for
    running the app against the fake server."""
    from ..aprs import TOCALL
    from ..ax25 import Frame
    def ui(src, text):
        return Frame.from_monitor(f"{src}>{TOCALL},WIDE1-1:{text}").encode()
    stations = [
        Station(144_800_000, [ui("M0TST-9", "!5530.00N/00410.00W>synthetic tracker")] * 50, -35, 0.5, 6.0),
        Station(144_925_000, [ui("GB7TST", ">synthetic node")] * 50, -40, 1.5, 9.0, offset_hz=400),
        Station(144_937_500, [ui("M0ABC", ">synthetic chat")] * 50, -45, 2.5, 11.0, offset_hz=-300),
        Station(145_825_000, [ui("RS0ISS", ">synthetic ISS")] * 50, -50, 3.5, 13.0, offset_hz=2_000),
    ]
    return SyntheticSource(centre, rate, stations, noise_dbfs=-35.0, seconds=seconds)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="A fake rtl_tcp server serving a synthetic 2 m band.")
    ap.add_argument("--port", type=int, default=1234)
    args = ap.parse_args(argv)
    server = RtlTcpServer(lambda c, r: demo_band(c, r), args.port)
    print(f"rtl_tcp (synthetic) on 127.0.0.1:{server.port}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.close()


if __name__ == "__main__":
    main()
