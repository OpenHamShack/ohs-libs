#!/usr/bin/env python3
"""Validate weights through the whole runtime on held-out synthetic
examples: the decoder as an app runs it (front end, AFC, network,
squelch), not the network alone as train.py's validation does.

    python3 tools/cw_neural/val.py --weights best.npz [--n 400] [--seed 77] [--afc on|off|both]
            [--snr-db 6] [--beds beds.npy] [--jobs 4]

Prints the character error rate by SNR band, calls copied as a word
(each example's first callsign-shaped word, as the bench scores RBN
calls), and characters a minute printed on examples with no wanted
signal. Seeds here are never used for training shards, so this is the
place to choose settings (AFC, squelch threshold, a space bonus) without
looking at the decoder bench's clips, which are the test set.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

CALL = re.compile(r"^(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9][A-Z0-9]{0,3}[A-Z]$")


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def one(k: int, seed: int, weights: str, afc: bool, snr_db: float, beds: str | None, space: float | None) -> dict:
    import synth
    from shack_cw.decoder import Decoder
    rng = np.random.default_rng([seed, k])
    bed = None
    if beds:
        b = np.load(beds, mmap_mode="r")
        bed = np.asarray(b[int(rng.integers(len(b)))])
    e = synth.example(rng, noise_bed=bed, want_audio=True)
    audio = e.feat
    d = Decoder(None, freq=synth.PITCH, polarity=False, engine="nn", weights=weights, afc=afc, snr_db=snr_db)
    if space is not None:
        d.space_bonus = space
    blk = int(0.075 * synth.FS)
    text = "".join(ev["text"] for i in range(0, len(audio), blk) for ev in d.feed(audio[i:i + blk], synth.FS))
    want = e.text.upper()
    calls = [w for w in want.split() if CALL.match(w.split("/")[0]) or any(CALL.match(p) for p in w.split("/"))]
    call = calls[0] if calls else None
    home = max(call.split("/"), key=len) if call else None
    return {"kind": e.kind, "snr": e.snr, "want": want, "got": text, "err": lev(text.strip(), want),
            "n": len(want), "call": home, "word": bool(home and any(home in w.split("/") for w in text.split())),
            "any": bool(home and home in text.replace(" ", "")), "chars": len(text.replace(" ", ""))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--weights", required=True)
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=77)
    ap.add_argument("--afc", choices=["on", "off", "both"], default="on")
    ap.add_argument("--snr-db", type=float, default=6.0)
    ap.add_argument("--space", type=float, help="log-probability bonus for a space (default: the engine's)")
    ap.add_argument("--beds")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--show", type=int, default=0)
    a = ap.parse_args(argv)
    for afc in ([True, False] if a.afc == "both" else [a.afc == "on"]):
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            rows = list(ex.map(one, range(a.n), [a.seed] * a.n, [a.weights] * a.n, [afc] * a.n,
                               [a.snr_db] * a.n, [a.beds] * a.n, [a.space] * a.n))
        sig = [r for r in rows if r["kind"] == "signal" and r["n"]]
        print(f"afc {'on' if afc else 'off'}, squelch {a.snr_db} dB, space bonus {a.space}: {a.weights}")
        for lo, hi, name in [(-99, -5, "<-5"), (-5, 5, "-5..5"), (5, 15, "5..15"), (15, 99, ">=15")]:
            g = [r for r in sig if lo <= r["snr"] < hi]
            c = [r for r in g if r["call"]]
            print(f"  snr {name:6s} n={len(g):3d}  CER {sum(r['err'] for r in g) / max(1, sum(r['n'] for r in g)):.3f}"
                  f"  calls {sum(r['word'] for r in c)}/{sum(r['any'] for r in c)} of {len(c)}")
        c = [r for r in sig if r["call"]]
        print(f"  all        n={len(sig):3d}  CER {sum(r['err'] for r in sig) / max(1, sum(r['n'] for r in sig)):.3f}"
              f"  calls {sum(r['word'] for r in c)}/{sum(r['any'] for r in c)} of {len(c)}")
        empty = [r for r in rows if r["kind"] != "signal"]
        print(f"  no wanted signal: {sum(r['chars'] for r in empty)} characters in {len(empty)} x 16 s"
              f" = {60 * sum(r['chars'] for r in empty) / max(1, 16 * len(empty)):.2f} a minute")
        for r in sig[:a.show]:
            print(f"   {r['snr']:5.1f} want {r['want'][:70]!r}\n         got  {r['got'][:70]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
