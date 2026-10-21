"""`python3 -m shack_packet.sdr.bench [--seconds 10] [--rate 2400000]`: the
SDR monitor's CPU, measured.

For 5 channels (the default plan) and 16 (the whole UK 2 m packet section,
144.800 to 144.9875 at 12.5 kHz), with both demodulators and with one,
on noise alone (the receivers' worst case: every slicer's bit clock chases
every noise transition) and with a station sending once a second on every
channel. The IQ is generated first and not counted; what is timed is everything the
monitor does with it - channeliser, receivers, levels, tables and
waterfall - as process time on one core, divided by the seconds of IQ,
the least of `--repeat` runs.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from ..aprs import TOCALL
from ..ax25 import Frame
from .memory import keep_heap
from .pipeline import Pipeline
from .plan import Channel, ChannelPlan, default_plan
from .synth import Station, SyntheticSource


def section_plan() -> ChannelPlan:
    return ChannelPlan(tuple(Channel(f"ch{i}", 144_800_000 + 12_500 * i, f"{144.8 + 0.0125 * i:.4f}", "node")
                             for i in range(16)))


def _iq(rate, centre, plan, seconds, traffic):
    stations = []
    if traffic:
        for i, c in enumerate(plan):
            frame = Frame.from_monitor(f"M0B{i % 10}>{TOCALL},WIDE1-1:!5530.00N/00410.00W>bench traffic {i}").encode()
            # One a second on every channel, the last well before the end.
            stations.append(Station(c.hz, [frame] * int(seconds - 1), -40, 0.1 + 0.03 * i, 1.0))
    src = SyntheticSource(centre, rate, stations, noise_dbfs=-35, seconds=seconds, seed=7)
    with src:
        return [b for b in iter(src.read, None)]


def measure(rate: float, plan: ChannelPlan, blocks, seconds: float, diversity) -> tuple[float, int]:
    centre = plan.centre_for(rate)
    pipe = Pipeline(rate, centre, plan, diversity=diversity)
    t = time.process_time()
    for b in blocks:
        pipe.process(b)
    pipe.flush()
    return (time.process_time() - t) / seconds, len(pipe.monitor.frames)


def channeliser_only(rate: float, plan: ChannelPlan, seconds: float) -> float:
    from .channeliser import Channeliser
    centre = plan.centre_for(rate)
    ch = Channeliser(rate, centre, list(plan))
    z = (np.random.default_rng(1).standard_normal(2 * int(rate)) * 0.01).astype(np.float32).view(np.complex64)
    block = int(rate * 0.05)
    t = time.process_time()
    for _ in range(int(seconds)):
        for i in range(0, len(z), block):
            ch.feed(z[i:i + block])
    return (time.process_time() - t) / int(seconds)


def cu8_only(rate: float, seconds: float) -> float:
    """The RTL-SDR's and rtl_tcp's own cost: 8-bit samples to complex64."""
    from .source import cu8_to_complex
    raw = np.random.default_rng(2).integers(0, 256, 2 * int(rate * 0.05), dtype=np.uint8).tobytes()
    t = time.process_time()
    for _ in range(int(seconds * 20)):
        cu8_to_complex(raw)
    return (time.process_time() - t) / int(seconds)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python3 -m shack_packet.sdr.bench")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=2_400_000)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args(argv)
    heap = keep_heap()                   # as Pipeline does; memory.py says why
    rows = []
    for name, plan in (("5 channels", default_plan()), ("16 channels", section_plan())):
        rows.append((name, "channeliser alone",
                     min(channeliser_only(args.rate, plan, args.seconds) for _ in range(args.repeat)), None))
        for traffic in (False, True):
            blocks = _iq(args.rate, plan.centre_for(args.rate), plan, args.seconds, traffic)
            for diversity in (True, False):
                # The least of a few runs: other work on the machine only
                # ever adds time.
                runs = [measure(args.rate, plan, blocks, args.seconds, diversity) for _ in range(args.repeat)]
                label = f"{'both demodulators' if diversity else 'one demodulator'}, " \
                        f"{'a station on every channel' if traffic else 'noise only'}"
                rows.append((name, label, min(r[0] for r in runs), runs[0][1]))
    rows.append(("any", "RTL-SDR / rtl_tcp samples to complex", cu8_only(args.rate, args.seconds), None))
    width = max(len(r[1]) for r in rows)
    for name, label, cpu, frames in rows:
        extra = "" if frames is None else f"  ({frames} frames)"
        print(f"{name:12} {label:{width}}  {cpu * 1000:6.0f} ms/s  {cpu * 100:5.1f} % of a core{extra}")
    print(f"glibc heap kept (memory.py): {heap}")


if __name__ == "__main__":
    main()
