#!/usr/bin/env python3
"""Write synthetic training shards (synth.example) in parallel.

    python3 tools/cw_neural/data.py OUT_DIR --examples 60000 --shard 1000 --seed 1 --jobs 6
            [--beds beds.npy]

Each shard is an .npz: `sums` (N, frames, columns, 2) float16, the
decoder front end's complex sums at offsets(front) (pack()), with `front`
naming it; `labels` (SYMBOLS indices, concatenated) with `lab_off`
(N + 1), `wpm`, `t_first`, `t_last`, `snr`, `kind`. Shard k is made from
seed (seed, k), so a set of shards is the same whoever makes it, and a
validation set made with another seed never shares an example with
training. Round 1's shards held one front's features (`feat`) instead;
train.py reads both, but sums let one set of examples train any front
end whose frequencies it holds (round 2 compared two on the same data).

Large: about 140 kB an example. Keep shards out of git (shack ~/build,
office /srv/iq/derived/cw-neural).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))


def shard(out: str, k: int, n: int, seed: int, seconds: float, beds: str | None, profile: str = "all") -> str:
    import synth
    dst = Path(out) / f"shard-{seed}-{k:05d}.npz"
    if dst.is_file():
        return f"skip {dst.name}"
    rng = np.random.default_rng([seed, k])
    bed = np.load(beds, mmap_mode="r") if beds else None
    sums, labels, off, meta = [], [], [0], {"wpm": [], "t_first": [], "t_last": [], "snr": [], "kind": []}
    t0 = time.time()
    for _ in range(n):
        if bed is not None:
            b = bed[int(rng.integers(len(bed)))]
            e = synth.example(rng, seconds, noise_bed=np.asarray(b), profile=profile)
        else:
            e = synth.example(rng, seconds, profile=profile)
        sums.append(pack(e.feat))
        labels += e.label
        off.append(len(labels))
        for key in meta:
            meta[key].append(getattr(e, key))
    tmp = dst.with_suffix(".tmp.npz")
    np.savez(tmp, sums=np.stack(sums), front=np.array(synth.SUMS_FRONT), labels=np.array(labels, np.int16),
             lab_off=np.array(off, np.int64), **{k: np.array(v) for k, v in meta.items()})
    os.replace(tmp, dst)
    return f"{dst.name} {n} in {time.time() - t0:.0f} s"


def pack(c: np.ndarray) -> np.ndarray:
    """Complex sums (frames, columns) -> float16 (frames, columns, 2),
    scaled so the largest is 1. Features are log ratios to the noise
    beside the tone, so the scale goes; float16 keeps each value to 0.05 %
    down to 84 dB under the largest (a 60 dB signal's noise is ~70 dB
    under it in one frame), and coarser below that."""
    s = float(np.abs(c).max()) or 1.0
    c = c / s
    return np.stack([c.real, c.imag], axis=-1).astype(np.float16)


def unpack(p: np.ndarray) -> np.ndarray:
    """pack()'s float16 (..., frames, columns, 2) -> complex128."""
    p = np.asarray(p, np.float64)
    return p[..., 0] + 1j * p[..., 1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--examples", type=int, default=60000)
    ap.add_argument("--shard", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--first", type=int, default=0, help="first shard number (to add to a set)")
    ap.add_argument("--seconds", type=float, default=16.0)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--beds", help="real band noise: (beds, samples) complex64 at 8 kHz")
    ap.add_argument("--profile", choices=["all", "weak", "cochan"], default="all", help="synth.example's profile")
    a = ap.parse_args(argv)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    n = -(-a.examples // a.shard)
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(shard, a.out, a.first + k, a.shard, a.seed, a.seconds, a.beds, a.profile)
                for k in range(n)]
        for f in cf.as_completed(futs):
            print(f.result(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
