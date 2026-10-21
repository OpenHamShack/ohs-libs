"""What the neural engine costs one core, beside the classic engine.

    python3 -m shack_cw.nn.cpu [--seconds 60] [--channels 1,16,64,128]

The skimmer's budget (CW-SKIMMER-PLAN §2, Brian 13/10/2026) is about
0.4 % of an i5-9400T core a channel (4 ms of CPU a second of signal),
ideally 0.15 %. Two numbers matter:

* one decoder fed audio (Decoder.feed, 8 kHz, 75 ms blocks), as Radio's
  CW receiver or the decoder bench runs it: front end and engine;
* a skimmer channel: the engine alone on ready-made sums (the filter
  bank's Gather), every channel in one Bank call a block - what each
  extra channel costs once the band's front end has run. The classic
  engine's figure is shack_cw.bench.backend_us (feed_frames).

Single-threaded (numpy's BLAS is told so before it loads); the best of
--repeat runs, as process time.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402

from ..bench import backend_us  # noqa: E402
from ..decoder import Decoder  # noqa: E402
from ..morse import envelope  # noqa: E402
from . import Bank, load  # noqa: E402

FS = 8000


def _audio(seconds: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    env = envelope(" ".join(["CQ CQ DE G4ABC G4ABC K"] * 40), 20, FS)
    env = np.concatenate([env, np.zeros(max(0, n - len(env)))])[:n]
    env[n // 2:] = 0.0
    t = np.arange(n) / FS
    return (0.3 * env * np.sin(2 * np.pi * 700 * t) + 0.05 * rng.standard_normal(n)).astype(np.float32)


def feed_us(engine: str, seconds: float, repeat: int, weights=None) -> float:
    """CPU microseconds a second of audio for one Decoder.feed."""
    x = _audio(seconds)
    blk = int(0.075 * FS)
    best = 1e18
    for _ in range(repeat):
        d = Decoder(None, freq=700.0, polarity=False, engine=engine, **({"weights": weights} if weights else {}))
        c0 = time.process_time()
        for i in range(0, len(x), blk):
            d.feed(x[i:i + blk], FS)
        best = min(best, time.process_time() - c0)
    return 1e6 * best / seconds


def bank_us(channels: int, seconds: float, repeat: int, seed: int = 0, weights=None, lm: float = 0.0) -> float:
    """CPU microseconds a second of signal per channel, for `channels`
    channels through one Bank in 75 ms blocks (15 frames)."""
    rng = np.random.default_rng(seed)
    n = int(seconds / 0.005)
    env = envelope(" ".join(["CQ CQ DE G4ABC G4ABC K"] * 40), 20, 200.0, edge=0.015)   # three frames
    key = np.concatenate([env, np.zeros(max(0, n - len(env)))])[:n]
    cols = len(Bank(6.0, weights).offsets)
    c = (rng.standard_normal((n, channels, cols)) + 1j * rng.standard_normal((n, channels, cols))).astype(np.complex64)
    c[:, :, 0] += 4.0 * key[:, None]
    keys = list(range(channels))
    best = 1e18
    for _ in range(repeat):
        b = Bank(6.0, weights)
        b.lm = lm
        c0 = time.process_time()
        for i in range(0, n, 15):
            b.run(c[i:i + 15], keys)
        best = min(best, time.process_time() - c0)
    return 1e6 * best / seconds / channels


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--channels", default="1,16,64,128")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json")
    ap.add_argument("--weights", help="weights other than the shipped ones")
    ap.add_argument("--lm", type=float, default=0.0, help="the skimmer channels with the language model's search "
                    "at this weight (nn.lm); 0 is the greedy reading")
    a = ap.parse_args(argv)
    net = load(a.weights)
    rows = {"classic feed": feed_us("classic", a.seconds, a.repeat),
            "nn feed": feed_us("nn", a.seconds, a.repeat, a.weights),
            "classic skimmer channel (feed_frames)": min(backend_us(None, a.seconds) for _ in range(a.repeat))}
    for k in (int(x) for x in a.channels.split(",")):
        rows[f"nn skimmer channel, bank of {k}"] = bank_us(k, a.seconds, a.repeat, weights=a.weights)
        if a.lm:
            rows[f"  the same with the LM at {a.lm:g}, bank of {k}"] = bank_us(k, a.seconds, a.repeat,
                                                                             weights=a.weights, lm=a.lm)
    print(f"network: {net.params} parameters, width {net.width}, dilations {net.dil}")
    print(f"{'':44s} {'ms/s':>7s} {'% core':>7s}")
    for k, us in rows.items():
        print(f"{k:44s} {us / 1000:7.2f} {us / 1e4:7.3f}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({k: {"ms_per_s": v / 1000, "pct_core": v / 1e4} for k, v in rows.items()}, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
