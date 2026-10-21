#!/usr/bin/env python3
"""Build the CW language model (shack_cw.nn.lm) from real text.

    python3 tools/cw_lm/build.py OUT.npz --analysis ANALYSIS.json --calls CALLS.txt
            [--hold-fold 0|1] [--skip-group G ...] [--order 6] [--copies 40] [--seed 1]

Text: the CW survey's `agree` windows (office
/srv/iq/derived/cw-survey/analysis.json, ohs-libs research/cw-survey):
15 s of a real line where the classic engine and the network printed the
same thing to within one character in ten. Consecutive agreed windows of
a line are one run of text. The engines glue and split words alike
("CQCQ CQDEIZ"), so each run is re-spaced first by the skimmer's own
calls.segment(); then every call-shaped word is masked. A run is copied
--copies times, each mask filled with a call drawn from CALLS (one call
and its spot count a line, e.g. every CW call the RBN spotted in a week
that is neither the survey's nor the dev set's), weighted by the square
root of its count. So the model learns the shape of real overs and the
shape of real calls, but not which station said what: a held-out line's
own call reaches it only if it is on the call list, as it would be on
the air.

Held out, always: the test set's recordings (the decoder bench's: the
Kiwi corpus, ohs-box's 20 m skimmer recording and four Flex surveys).
`--skip-group kiwi-cwval` holds out the dev set; `--hold-fold F` holds out
the survey recordings in fold F (fold(rec), crc32 mod 2), so a model is
never scored on the text it was fitted on.

Also in the text: the skimmer's CW vocabulary (calls.STOP and CQ_MODS)
as single words, so the rarer ones (QTH, NAME, RST, SOTA) have a shape
even where the survey never agreed on them.

The model: interpolated Kneser-Ney over the network's symbols (spaces
included; prosigns are one symbol each), order 6 (five symbols of
context: a whole call's prefix and digit, or "DE " and a prefix). A
context is kept if seen --min-count times; a lookup that misses falls to
the next shorter one. Stored is each kept context's whole next-symbol
distribution (float16), so the search does one dictionary lookup a beam.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

# The decoder bench (the test set) was cut from these; their text never
# reaches a model, so a future look at the test set stays honest.
TEST_GROUPS = ("kiwi-corpus", "box-20m-skimmer")
TEST_RECS = ("20261012-flex-12m-loop-survey", "20261012-flex-17m-loop-survey", "20261012-flex-30m-loop-survey",
             "20261012-flex-60m-loop-survey")
MASK = -1
CALL_RE = re.compile(r"^(?=.*\d)[A-Z0-9/]{3,14}$")


def fold(rec: str) -> int:
    return zlib.crc32(rec.encode()) % 2


def held_out(rec: str, group: str, hold_fold: int | None, skip_groups=()) -> bool:
    return (group in TEST_GROUPS or rec in TEST_RECS or group in skip_groups
            or (hold_fold is not None and fold(rec) == hold_fold))


def runs(analysis: dict, hold_fold: int | None, skip_groups=()) -> list[str]:
    """The agreed text of each line as runs of consecutive windows."""
    by = defaultdict(list)
    for w in analysis["windows"]:
        if w["cls"] == "agree" and not held_out(w["rec"], w["group"], hold_fold, skip_groups):
            by[(w["rec"], round(w["hz"]))].append(w)
    out = []
    for ws in by.values():
        ws.sort(key=lambda w: w["k"])
        cur, k0 = [], None
        for w in ws:
            if k0 is not None and w["k"] != k0 + 1 and cur:
                out.append("".join(cur))
                cur = []
            cur.append(w["nn"])
            k0 = w["k"]
        if cur:
            out.append("".join(cur))
    return out


def masked(text: str) -> list[str | int]:
    """Words of a run re-spaced as the skimmer would, calls masked."""
    from shack_cw.skim.calls import segment, shape_ok
    words = segment(text.split())
    return [MASK if (shape_ok(w) or (CALL_RE.match(w) and any(shape_ok(p) for p in w.split("/")))) else w
            for w in words]


def read_calls(path: str, min_spots: int) -> tuple[list[str], np.ndarray]:
    from shack_cw.skim.calls import shape_ok
    calls, n = [], []
    for ln in Path(path).read_text().splitlines():
        bits = ln.split()
        if not bits:
            continue
        c, k = bits[0].upper(), int(bits[1]) if len(bits) > 1 else 1
        if k >= min_spots and shape_ok(c):
            calls.append(c)
            n.append(k)
    w = np.sqrt(np.array(n, float))
    return calls, w / w.sum()


def kneser_ney(seqs: list[list[int]], order: int, nsym: int, min_count: int, discount: float = 0.75):
    """{context tuple: log-probabilities over 0..nsym-1} for every kept
    context, interpolated Kneser-Ney (symbol 0, CTC's blank, gets none)."""
    from shack_cw.nn import SPACE
    full = [defaultdict(lambda: defaultdict(int)) for _ in range(order)]     # k -> ctx -> sym -> count
    pad = (SPACE,) * (order - 1)
    for s in seqs:
        x = pad + tuple(s)
        for i in range(order - 1, len(x)):
            h = x[i - order + 1:i]
            full[order - 1][h][x[i]] += 1
    # Lower orders take continuation counts: how many distinct symbols
    # precede (ctx, sym), not how often it occurs.
    for k in range(order - 2, -1, -1):
        for h, d in full[k + 1].items():
            lo = h[1:]
            for sym in d:
                full[k][lo][sym] += 1
    probs: dict[tuple, np.ndarray] = {}
    uni = np.zeros(nsym)
    for sym, c in full[0][()].items():
        uni[sym] += c
    base = np.full(nsym, 1.0 / (nsym - 1))
    base[0] = 0.0
    p0 = (uni / uni.sum()) * 0.95 + base * 0.05
    probs[()] = p0
    for k in range(1, order):
        for h, d in full[k].items():
            tot = sum(d.values())
            if tot < min_count:
                continue
            lower = probs.get(h[1:])
            if lower is None:              # the shorter context was pruned: walk down
                j = 1
                while lower is None:
                    j += 1
                    lower = probs.get(h[j:])
            p = np.zeros(nsym)
            for sym, c in d.items():
                p[sym] = max(c - discount, 0.0) / tot
            p += (discount * len(d) / tot) * lower
            probs[h] = p
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--analysis", required=True, help="the CW survey's analysis.json")
    ap.add_argument("--calls", required=True, help="call list: CALL [spots] a line")
    ap.add_argument("--min-spots", type=int, default=3, help="calls spotted fewer times are left off (busts)")
    ap.add_argument("--hold-fold", type=int, choices=(0, 1))
    ap.add_argument("--skip-group", nargs="*", default=[])
    ap.add_argument("--order", type=int, default=6)
    ap.add_argument("--copies", type=int, default=40)
    ap.add_argument("--min-count", type=int, default=8)
    ap.add_argument("--vocab-copies", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    from shack_cw.nn import SYMBOLS, tokens
    from shack_cw.skim.calls import CQ_MODS, STOP
    rng = np.random.default_rng(a.seed)
    analysis = json.loads(Path(a.analysis).read_text())
    texts = runs(analysis, a.hold_fold, tuple(a.skip_group))
    calls, w = read_calls(a.calls, a.min_spots)
    runs_m = [masked(t) for t in texts]
    n_mask = sum(x == MASK for r in runs_m for x in r)
    draws = iter(rng.choice(len(calls), size=n_mask * a.copies, p=w).tolist())
    seqs: list[list[int]] = []
    for r in runs_m:
        for _ in range(a.copies):
            words = [calls[next(draws)] if x == MASK else x for x in r]
            seqs.append(tokens(" ".join(words)))
    vocab = sorted(STOP | CQ_MODS)
    for _ in range(a.vocab_copies):
        seqs.append(tokens(" ".join(vocab[int(i)] for i in rng.permutation(len(vocab)))))
    probs = kneser_ney(seqs, a.order, len(SYMBOLS), a.min_count)
    from shack_cw.nn.lm import _code
    keys = sorted(probs, key=_code)
    with np.errstate(divide="ignore"):
        logp = np.log(np.stack([probs[k] for k in keys])).astype(np.float16)
    logp[:, 0] = -np.inf
    meta = {"symbols": SYMBOLS, "order": a.order, "runs": len(texts), "chars": sum(len(t) for t in texts),
            "masked_calls": n_mask, "calls": len(calls), "copies": a.copies, "hold_fold": a.hold_fold,
            "skip_groups": a.skip_group, "min_count": a.min_count, "call_list": Path(a.calls).name}
    np.savez_compressed(a.out, meta=np.array(json.dumps(meta)), codes=np.array([_code(k) for k in keys], np.int64),
                        logp=logp, calls=np.frombuffer("\n".join(calls).encode(), np.uint8))
    print(json.dumps(dict(meta, symbols=None, contexts=len(keys),
                          kb=Path(a.out).stat().st_size // 1024)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
