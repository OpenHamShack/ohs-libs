"""Measures the fast modes (fsk.py) through the Pluto's own IQ path: how
wide they are, how many frames come through noise and a frequency error,
and what receiving costs.

    python3 -m shack_packet.fsk_bench                       # every table, a few minutes
    python3 -m shack_packet.fsk_bench --only cpu --frames 40

The loopback is the real transmit and receive code, minus only the radio:
fsk.modulate -> radios.pluto.FskModulator (the IQ a fast-mode PlutoLink
pushes, at FSK_RATE_IQ with the channel `IFO_HZ` above the LO) -> complex
white noise -> radios.sdrrx.FskChannelReceiver (the channeliser and
discriminator a fast-mode PlutoLink receives through) -> fsk.Receiver.
Each frame is its own short over with a 30 ms preamble, after a second of
noise alone (the receiver's floor), so every frame has to be acquired
afresh: the hard case, not a long stream that is locked once.

**CNR** is the carrier's power over the noise's in the signal's occupied
(99 %) bandwidth, fsk.occupied_hz: 10.4 kHz at 9600 baud. Eb/N0 is CNR +
10 log10(occupied / baud), 0.35 dB more. `occupied` is the transmitted
spectrum itself: its 99 % bandwidth, and how much of it falls in the next
25 kHz channel (beyond +-12.5 kHz).

`cpu` is process time per second of IQ for the whole receive path, block
by block as PlutoLink feeds it (50 ms), and for the transmitter. Real time
is 1000 ms a second, on one core: anything well under keeps up.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import time

import numpy as np

from . import ax25, modem
from .radios.pluto import FSK_RATE_IQ, FSK_SPS, RX_BLOCK_S, WIDE_IFO_HZ, WIDE_OCCUPIED_HZ, FskModulator
from .radios.sdrrx import FskChannelReceiver

IFO_HZ = 100_000.0
CHANNEL_HZ = 432_650_000.0
CNRS = (8, 10, 11, 12, 14)
#: 4-FSK's threshold is about 5 dB higher (module docstring).
CNRS_4 = (12, 14, 16, 18, 20)
OFFSETS = (0.0, 3_000.0, -3_000.0)
#: Every fast mode, slowest first.
MODES = tuple(m for m in modem.MODES.values() if m.fast)


def ifo_hz(mode) -> float:
    """Where a PlutoLink puts this mode's channel above its LO."""
    return WIDE_IFO_HZ if modem.mode(mode).occupied_hz > WIDE_OCCUPIED_HZ else IFO_HZ


def _frames(n: int, rng) -> list[bytes]:
    return [ax25.Frame.from_monitor(f"GM4BRI-{i % 15}>APZOHS:>{'x' * int(rng.integers(20, 200))} {i}").encode()
            for i in range(n)]


def _mod(mode, rate: int, offset_hz: float = 0.0, ifo: float | None = None) -> FskModulator:
    m = modem.mode(mode)
    return FskModulator(FSK_RATE_IQ, rate, m.deviation_hz, (ifo_hz(m) if ifo is None else ifo) + offset_hz,
                        amplitude=1.0)


def over_iq(frames, mode, offset_hz: float = 0.0, *, txdelay_ms: float = 30.0, gap_s: float = 0.02,
            lead_s: float = 1.0) -> np.ndarray:
    """Frames as separate overs, each its own transmission, in the IQ a
    fast-mode PlutoLink pushes (unit amplitude), `offset_hz` off frequency,
    after `lead_s` of nothing. `mode`: a modem.Mode, name or baud."""
    m = modem.mode(mode)
    rate = m.baud * FSK_SPS
    parts = [np.zeros(int(rate * lead_s))]
    for f in frames:
        parts.append(modem.modulate([f], rate, mode=m, txdelay_ms=txdelay_ms, tail_ms=5))
        parts.append(np.zeros(int(rate * gap_s)))
    bb = np.concatenate(parts)
    iq = _mod(m, rate, offset_hz)(bb)
    iq[:int(FSK_RATE_IQ * lead_s)] = 0.0                     # no carrier in the lead
    return iq.astype(np.complex64)


def add_noise(iq: np.ndarray, mode, cnr_db: float, rng) -> np.ndarray:
    """Complex white noise at `cnr_db` below a unit carrier in the occupied
    bandwidth (module docstring)."""
    per_hz = 10 ** (-cnr_db / 10.0) / modem.mode(mode).occupied_hz
    sigma = np.sqrt(per_hz * FSK_RATE_IQ / 2.0)
    noise = rng.normal(0, sigma, len(iq)) + 1j * rng.normal(0, sigma, len(iq))
    return (iq + noise).astype(np.complex64)


def receive(iq: np.ndarray, mode) -> tuple[list, float]:
    """The fast-mode PlutoLink's receive path over `iq`, in its own block
    size: (frames heard, process seconds per second of IQ)."""
    m = modem.mode(mode)
    ifo = ifo_hz(m)
    chan = FskChannelReceiver(FSK_RATE_IQ, CHANNEL_HZ - ifo, CHANNEL_HZ, baud=m.baud, sps=FSK_SPS,
                              deviation_hz=m.deviation_hz, occupied_hz=m.occupied_hz)
    rx = modem.receiver(chan.rate, mode=m, block_ms=20.0)
    block = int(FSK_RATE_IQ * RX_BLOCK_S)
    heard = []
    t0 = time.process_time()
    for at in range(0, len(iq), block):
        heard += rx.feed(chan.hear(iq[at:at + block]))
    heard += rx.flush()
    return heard, (time.process_time() - t0) / (len(iq) / FSK_RATE_IQ)


def loopback(mode, cnr_db: float, offset_hz: float = 0.0, n: int = 20, seed: int = 1) -> tuple[int, int, float]:
    """(frames decoded, frames sent, receive CPU per second of IQ)."""
    rng = np.random.default_rng(seed)
    frames = _frames(n, rng)
    iq = add_noise(over_iq(frames, mode, offset_hz), mode, cnr_db, rng)
    heard, cpu = receive(iq, mode)
    return len({h.frame for h in heard} & set(frames)), n, cpu


def spectrum(mode, shape: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(frequency, power) of random data in `mode` through the transmitter,
    centred on the carrier."""
    m = modem.mode(mode)
    if shape is not None:
        m = dataclasses.replace(m, shape=shape)
    rng = np.random.default_rng(3)
    rate = m.baud * FSK_SPS
    frames = [bytes(rng.integers(0, 256, 200).astype(np.uint8)) for _ in range(30)]
    bb = modem.modulate(frames, rate, mode=m, txdelay_ms=10)
    iq = _mod(m, rate, ifo=0.0)(bb)
    n = 1 << 16
    power = np.zeros(n)
    for i in range(len(iq) // n):
        power += np.abs(np.fft.fft(iq[i * n:(i + 1) * n] * np.hanning(n))) ** 2
    return np.fft.fftshift(np.fft.fftfreq(n, 1 / FSK_RATE_IQ)), np.fft.fftshift(power)


def occupied(mode, shape: str | None = None, channel_hz: float = 25_000.0) -> tuple[float, float]:
    """(99 % bandwidth in Hz, dB of the power outside a `channel_hz` wide
    channel) of random data in `mode` through the transmitter."""
    f, power = spectrum(mode, shape)
    c = np.cumsum(power) / power.sum()
    width = f[np.searchsorted(c, 0.995)] - f[np.searchsorted(c, 0.005)]
    outside = 10 * np.log10(power[np.abs(f) > channel_hz / 2].sum() / power.sum())
    return float(width), float(outside)


#: The channel each mode is judged against: a 25 kHz channel for 9600,
#: 50 and 100 kHz above it (wider than any UK plan offers at 19 200 and
#: 38 400, but what they need), the 150 kHz 23 cm data channel, and the
#: 200 kHz 13 cm high-speed data segment for 153 600 baud GFSK.
CHANNELS = {"g3ruh9600": 25_000.0, "g3ruh19200": 50_000.0, "g3ruh38400": 100_000.0, "gfsk76800": 150_000.0,
            "gfsk153600": 200_000.0, "4fsk38400": 150_000.0, "4fsk76800": 150_000.0}


def table_occupied() -> None:
    print("\noccupied bandwidth (99 %), and the power outside the channel it is meant for")
    print(f"{'mode':>11} {'kbit/s':>7} {'deviation':>10} {'occupied':>9} {'channel':>8} {'outside':>8}")
    for m in MODES:
        ch = CHANNELS[m.name]
        w, out = occupied(m, channel_hz=ch)
        print(f"{m.name:>11} {m.bitrate / 1e3:>7.1f} {m.deviation_hz / 1e3:>6.1f} kHz {w / 1e3:>5.1f} kHz "
              f"{ch / 1e3:>4.0f} kHz {out:>5.1f} dB", flush=True)


def table_loopback(frames: int) -> None:
    print(f"\nIQ loopback: frames decoded of {frames}, each its own over (CNR in the occupied bandwidth)")
    for m in MODES:
        cnrs = CNRS_4 if m.bits == 2 else CNRS
        print(f"{m.name:>11} {'offset':>8} " + " ".join(f"{c:>5} dB" for c in cnrs))
        for off in OFFSETS:
            cells = [loopback(m, c, off, frames, seed=int(c * 10 + abs(off) + (off < 0)))[0] for c in cnrs]
            print(f"{'':>11} {off:>+8.0f} " + " ".join(f"{x:>8}" for x in cells), flush=True)


def table_cpu(frames: int) -> None:
    print(f"\nCPU, ms a second of IQ at {FSK_RATE_IQ / 1e6:.4f} MS/s, one core (real time is 1000)")
    print(f"{'mode':>11} {'receive':>8} {'transmit':>9}")
    for m in MODES:
        rng = np.random.default_rng(9)
        fr = _frames(frames, rng)
        iq = add_noise(over_iq(fr, m, lead_s=0.2), m, 25, rng)
        _, rx_cpu = receive(iq, m)
        rate = m.baud * FSK_SPS
        bb = modem.modulate(fr, rate, mode=m)
        mod = _mod(m, rate)
        piece = int(rate * 0.02)
        t0 = time.process_time()
        for at in range(0, len(bb), piece):
            mod(bb[at:at + piece])
        tx_cpu = (time.process_time() - t0) / (len(bb) / rate)
        print(f"{m.name:>11} {rx_cpu * 1000:>8.0f} {tx_cpu * 1000:>9.0f}", flush=True)


# -- throughput: the engine on the simulated channel -----------------------------------------

#: The Pluto's turnaround as the tuned settings assume it: the key's two
#: attribute writes (3 ms read back on the air) and the first push. Not
#: measured on RF yet; the keyed test with --turnaround-ms says.
TUNED_TURNAROUND_S = 0.03


def transfer(mode, kb: int = 100, *, window: int = 16, paclen: int = 256, txdelay_s: float = 0.2,
             turnaround_s: float = 0.1, timing=None, srej: bool = False, ber: float = 0.0, seed: int = 1,
             max_tx_s: float = 6.0) -> tuple[bool, float]:
    """`kb` KB from one engine to another over the frames-mode simulated
    channel with a Pluto's timing (sim.RadioTiming, default sim.PLUTO),
    as fastfsk_test's transfer does it: (all acknowledged, seconds from
    the first byte sent to the last acknowledged)."""
    import random

    from .channel import ChannelConfig
    from .datalink import LinkConfig
    from .engine import Engine
    from .identity import Station
    from .sim import PLUTO, Medium, SimClock, Simulation
    m = modem.mode(mode)
    clock = SimClock()
    medium = Medium(clock, "frames", baud=m.bitrate, ber=ber, seed=seed, dcd_lag_s=0.01,
                    min_lock_s=m.lock_bits / m.bitrate)
    sim = Simulation(clock, medium)
    engines = []
    for call in ("GM4BRI-1", "GM4BRI-2"):
        radio = medium.radio(call, turnaround_s=turnaround_s, timing=timing or PLUTO)
        cfg = ChannelConfig(txdelay_s=txdelay_s, txtail_s=0.01, persist=255, slottime_s=0.05,
                            max_frames=max(8, window + 1), max_tx_s=max_tx_s, mode=m.name)
        link = LinkConfig(k=window, n1=paclen, modulo=128 if window > 7 else 8, srej=srej)
        engines.append(Engine(Station.of(call, tx_enabled=True), radio, channel=cfg, link=link, clock=clock,
                              rng=random.Random(f"{seed}-{call}")))
        sim.add(engines[-1])
    a, b = engines
    got = bytearray()
    b.listen("GM4BRI-2", lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    conn = a.connect("GM4BRI-2")
    if not sim.run(30, lambda: conn.connected):
        return False, float("inf")
    data = random.Random(seed).randbytes(kb * 1024)
    start = clock()
    conn.send(data)
    ok = sim.run(900, lambda: not conn.pending and len(got) >= len(data))
    return ok and bytes(got) == data, clock() - start


def tuned(mode) -> dict:
    """The settings the README recommends for a mode (throughput table)."""
    from .radios.sdrrx import FskChannelReceiver
    from .sim import PLUTO
    m = modem.mode(mode)
    busy = FskChannelReceiver(FSK_RATE_IQ, CHANNEL_HZ - ifo_hz(m), CHANNEL_HZ, baud=m.baud, sps=FSK_SPS,
                              deviation_hz=m.deviation_hz, occupied_hz=m.occupied_hz).busy_average_s
    return dict(window=63, paclen=512, txdelay_s=0.0, turnaround_s=TUNED_TURNAROUND_S, srej=True,
                timing=dataclasses.replace(PLUTO, busy_tail_s=busy + RX_BLOCK_S))


#: As keyed on 18/10/2026 (fastfsk_test --window 16, PACLEN 256, TXDELAY
#: 200 ms, REJ, the Pluto's assumed 100 ms turnaround, 100 ms busy).
AS_KEYED = dict(window=16, paclen=256, txdelay_s=0.2, turnaround_s=0.1, srej=False)
BERS = (0.0, 1e-5, 1e-4)


def table_throughput(seeds: int) -> None:
    print("\nthroughput: 100 KB over the simulated channel with a Pluto's timing (sim.PLUTO), % of the bit rate,"
          f" mean of {seeds} seeds; BER is the channel's bit error rate")
    ok, t = transfer("g3ruh38400", 10, window=16, **{k: v for k, v in AS_KEYED.items() if k != "window"})
    print(f"  calibration: g3ruh38400, 10 KB as keyed: {t:.1f} s here, 5.5 s on the air (18/10/2026)")
    print(f"{'mode':>11} {'settings':>9} " + " ".join(f"{'BER ' + format(b, 'g'):>9}" for b in BERS))
    for m in MODES:
        for label, kw in (("as keyed", AS_KEYED), ("tuned", tuned(m))):
            cells = []
            for ber in BERS:
                runs = [transfer(m, 100, ber=ber, seed=s, **kw) for s in range(1, seeds + 1)]
                good = [100 * 1024 * 8 / t / m.bitrate for ok, t in runs if ok]
                cell = f"{100 * sum(good) / len(good):.0f} %" if good else "-"
                cells.append(cell + ("" if len(good) == len(runs) else "*"))
            print(f"{m.name:>11} {label:>9} " + " ".join(f"{c:>9}" for c in cells), flush=True)


# -- link adaptation on the simulated channel ------------------------------------------------


def fading(ab_dbhz: float, ba_dbhz: float, fade_db: float = 0.0, period_s: float = 10.0,
           fade_s: float = 4.0):
    """A Medium `quality`: C/N0 GM4BRI-1 -> GM4BRI-2 and back, each dropping
    by `fade_db` for `fade_s` of every `period_s`."""
    def quality(talker, listener, t):
        base = ab_dbhz if talker == "GM4BRI-1" else ba_dbhz
        return base - (fade_db if fade_db and t % period_s < fade_s else 0.0)
    return quality


def adaptive_transfer(modes, quality, kb: int = 100, seed: int = 1, window: int = 63, paclen: int = 512):
    """`kb` KB GM4BRI-1 -> GM4BRI-2 over the frames-mode channel with each
    frame's chance from its mode and the C/N0 (sim.frame_success), on
    radios that send `modes` (a ladder, adapt.py; one mode is that mode
    fixed, control frames included). (bit/s of data, the sending
    connection, the receiving one); bit/s 0 if it did not finish."""
    import random

    from .channel import ChannelConfig
    from .datalink import LinkConfig
    from .engine import Engine
    from .identity import Station
    from .sim import PLUTO, Medium, SimClock, Simulation
    clock = SimClock()
    medium = Medium(clock, "frames", seed=seed, dcd_lag_s=0.01, quality=quality, min_lock_s=0.001)
    sim = Simulation(clock, medium)
    engines = []
    for call in ("GM4BRI-1", "GM4BRI-2"):
        radio = medium.radio(call, turnaround_s=TUNED_TURNAROUND_S, timing=PLUTO, modes=tuple(modes))
        cfg = ChannelConfig(txdelay_s=0.0, txtail_s=0.01, persist=255, max_frames=window + 1, max_tx_s=6.0)
        link = LinkConfig(k=window, n1=paclen, modulo=128, srej=True)
        engines.append(Engine(Station.of(call, tx_enabled=True), radio, channel=cfg, link=link, clock=clock,
                              rng=random.Random(f"{seed}{call}")))
        sim.add(engines[-1])
    a, b = engines
    got = bytearray()
    b.listen("GM4BRI-2", lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    conn = a.connect("GM4BRI-2")
    if not sim.run(60, lambda: conn.connected):
        return 0.0, conn, None
    data = random.Random(seed).randbytes(kb * 1024)
    start = clock()
    conn.send(data)
    done = sim.run(1800, lambda: not conn.pending and len(got) >= len(data))
    theirs = next(iter(b.connections.values()), None)
    if not done or bytes(got[:len(data)]) != data:
        return 0.0, conn, theirs
    return len(data) * 8 / (clock() - start), conn, theirs


#: (name, quality) for the adaptation table: 18/10's on-air asymmetry
#: (the nano's direction could not carry 4fsk76800), the same 6 dB apart
#: with fades of 8 and 15 dB, and a weaker link with short fades.
ADAPT_CASES = (
    ("A->B 74, B->A 62 dB-Hz", fading(74, 62)),
    ("A->B 74, B->A 68", fading(74, 68)),
    ("74/68, 8 dB fades 4 s in 10", fading(74, 68, 8)),
    ("74/68, 15 dB fades 4 s in 10", fading(74, 68, 15)),
    ("66/60, 6 dB fades 3 s in 15", fading(66, 60, 6, 15, 3)),
)


def table_adapt(seeds: int) -> None:
    from . import adapt
    ladder = adapt.ladder()
    print(f"\nlink adaptation: 100 KB on the simulated channel, kbit/s of data, mean of {seeds} seeds; ladder "
          f"{' > '.join(ladder)}; k 63, PACLEN 512, SREJ, the Pluto's timing")
    print(f"{'channel':>30} {'adaptive':>9} {'fixed 4fsk76800':>16} {'fixed g3ruh9600':>16}")
    for name, quality in ADAPT_CASES:
        cells = []
        for modes in (ladder, ("4fsk76800",), ("g3ruh9600",)):
            rates = [adaptive_transfer(modes, quality, 100, seed)[0] for seed in range(1, seeds + 1)]
            cells.append(f"{sum(rates) / len(rates) / 1e3:.1f}" + ("*" if 0.0 in rates else ""))
        print(f"{name:>30} " + " ".join(f"{c:>{w}}" for c, w in zip(cells, (9, 16, 16))), flush=True)


TABLES = {"occupied": lambda a: table_occupied(), "loopback": lambda a: table_loopback(a.frames),
          "cpu": lambda a: table_cpu(a.frames), "throughput": lambda a: table_throughput(a.seeds),
          "adapt": lambda a: table_adapt(a.seeds)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default=",".join(TABLES), help="comma-separated: " + ", ".join(TABLES))
    parser.add_argument("--frames", type=int, default=30, help="frames a cell (default 30)")
    parser.add_argument("--seeds", type=int, default=3, help="throughput: seeds a cell (default 3)")
    args = parser.parse_args(argv)
    print(f"shack_packet fsk_bench on {os.uname().nodename}, numpy {np.__version__}")
    for name in args.only.split(","):
        TABLES[name.strip()](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
