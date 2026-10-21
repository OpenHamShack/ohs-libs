#!/usr/bin/env python3
"""Build the space model (shack_cw.nn.space): how likely a word space is
between what was printed and the next character, from synthetic overs.

    python3 tools/cw_neural/spacelm.py OUT.npz [--overs 400000] [--seed 5] [--min 8]

The overs are textgen's (CQs, contest exchanges, QSOs, beacons, call
lists and random groups, with calls built from cty.dat's prefixes), never
a real recording's text and never the RBN's calls, which are the decoder
bench's truth. What it learns is the shape of the language: a space after
CQ and DE, none between a call's prefix and its digit, a call's suffix
one to four letters long.

For every character of every over: the three symbols printed before the
gap in front of it (spaces included, the gap's own space not), the
character, and whether the gap holds a space. Each context's log-odds are
smoothed towards the next shorter context's (k = 5 pseudo-counts), and a
context is kept only if seen `--min` times and it moves the log-odds by a
quarter or more from its shorter context: the table stays a few hundred
kilobytes.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

K = 5.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--overs", type=int, default=400000)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--min", type=int, default=8)
    a = ap.parse_args(argv)
    import textgen
    from shack_cw.nn import SPACE, tokens
    from shack_cw.nn.space import N, code
    rng = np.random.default_rng(a.seed)
    counts = [defaultdict(lambda: [0, 0]) for _ in range(4)]       # order (symbols of history) -> code -> [space, all]
    for _ in range(a.overs):
        me = textgen.callsign(rng)
        t = tokens(textgen.over(rng, me=me if rng.random() < 0.7 else None))
        hist: list[int] = []
        sp = False
        for s in t:
            if s == SPACE:
                sp = True
                continue
            if hist:
                for k in range(4):
                    c = counts[k][code(hist[len(hist) - k:] if k else [], s)]
                    c[0] += sp
                    c[1] += 1
            if sp and hist:
                hist.append(SPACE)
            hist.append(s)
            sp = False
    tables = {}
    lower = None
    for k in range(4):
        keys, vals = [], []
        for c, (ns, nt) in counts[k].items():
            # The shorter context: drop the oldest symbol.
            p0 = 0.5 if k == 0 else 1 / (1 + np.exp(-lower[c % N ** k]))
            p = (ns + K * p0) / (nt + K)
            lo = float(np.log(max(p, 1e-4) / max(1 - p, 1e-4)))
            if k == 0 or (nt >= a.min and abs(lo - lower[c % N ** k]) >= 0.25):
                keys.append(c)
                vals.append(lo)
        full = dict(zip(keys, vals))
        if k:
            # Contexts dropped at this order fall back to the shorter one,
            # so the next order smooths towards what will actually be used.
            full = defaultdict(float, full)
            for c in counts[k]:
                if c not in full:
                    full[c] = lower[c % N ** k]
        lower = full
        o = np.argsort(keys)
        tables[f"k{k}"] = np.array(keys, np.int64)[o]
        tables[f"v{k}"] = np.array(vals, np.float32)[o]
        print(f"order {k}: {len(counts[k])} contexts seen, {len(keys)} kept")
    np.savez_compressed(a.out, **tables)
    print(f"-> {a.out} ({Path(a.out).stat().st_size // 1024} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
