#!/usr/bin/env python3
"""Real training examples from unlabelled recordings: the overs on which
the classic engine and the network agree, labelled with what they read.

    python3 tools/cw_neural/pseudo.py CORPUS_DIR OUT_DIR --weights W.npz [--jobs 6] [--max-cer 0]

Synthetic training stopped improving the network on real signals (r1 and
r2 read the dev set's 128 calls alike while synthetic validation kept
falling): what is left is how real bands differ from the generator. Real
CW has no transcript, but two engines that fail in different ways rarely
agree on a wrong letter, so where the classic engine's text and the
network's match (exactly, spaces aside, unless --max-cer allows more)
the text is taken as the label and the over becomes an example: real
fists, fading, static and neighbours. Allowing one error in ten let
"DL7JAN DLJAN" through as a label, so the default is exact agreement.
The labels are our own engines' output only (never another decoder's,
whose licence would then reach the weights).

CORPUS_DIR is a tools/cw_corpus.py recording folder (manifest.jsonl,
iq/*.wav) of recordings that are in neither the decoder bench (the test
set) nor realval's dev set. Every keyed line in each recording is cut
the bench's way (decoder_bench/clips.py: an 8 kHz channel on a 700 Hz
pitch through the bench receiver); overs are found from the network's
own emissions (gaps of 2.5 s or more), each window starting 2 s before
its first letter came out. Writes data.py's shard format (the front
end's sums), kind "real", with no speed target. --spaces also asks the
two engines to space the text alike: the label's spaces are otherwise
the network's own, and word spacing is what it gets wrong most.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "decoder_bench"))
sys.path.insert(0, str(HERE))

FS = 8000
OUT_S = 0.02              # the network's output frame
EXAMPLE_FRAMES = 3200     # 16 s of 5 ms frames, as the synthetic examples
OVER_GAP_S = 2.5
LEAD_S = 2.0
TAIL_S = 0.6


def lines(z: np.ndarray, rate: float, min_db: float = 10.0, span: float = 4400.0) -> list[float]:
    """Keyed lines in a recording: per ~6 Hz bin, the 95th percentile of
    power over 21 ms frames, standing min_db over the median bin and keyed
    (95th over 25th percentile by 6 dB); local maxima 80 Hz apart."""
    nfft, hop = 2048, 256
    fr = np.lib.stride_tricks.sliding_window_view(z, nfft)[::hop]
    if len(fr) > 6000:
        fr = fr[np.linspace(0, len(fr) - 1, 6000).astype(int)]
    P = np.abs(np.fft.fft(fr * np.hanning(nfft), axis=1)) ** 2
    f = np.fft.fftfreq(nfft, 1 / rate)
    p95, p25 = np.percentile(P, 95, axis=0), np.percentile(P, 25, axis=0)
    med = np.median(p95)
    ok = (10 * np.log10(p95 / med) > min_db) & (10 * np.log10(p95 / np.maximum(p25, 1e-30)) > 6) & (np.abs(f) < span)
    out: list[float] = []
    for i in np.argsort(-p95):
        if not ok[i]:
            continue
        if all(abs(f[i] - g) >= 80 for g in out):
            out.append(float(f[i]))
    return out


def raw_emissions(net, feat: np.ndarray) -> list[tuple[float, int]]:
    """The network's greedy CTC symbols over a whole stream, no squelch:
    (time the symbol came out, symbol)."""
    from shack_cw.nn import BLANK
    logp, _ = net.step(feat[None], net.state(1))
    best = logp[0].argmax(-1)
    prev = np.concatenate([[BLANK], best[:-1]])
    return [((j + 1) * OUT_S, int(best[j])) for j in np.flatnonzero((best != prev) & (best != BLANK))]


def overs(em: list[tuple[float, int]], max_s: float = 13.0) -> list[list[tuple[float, int]]]:
    groups: list[list] = []
    for e in em:
        if groups and e[0] - groups[-1][-1][0] < OVER_GAP_S:
            groups[-1].append(e)
        else:
            groups.append([e])
    out = []

    def split(g):
        if g[-1][0] - g[0][0] <= max_s:
            out.append(g)
            return
        gaps = [g[i + 1][0] - g[i][0] for i in range(len(g) - 1)]
        k = int(np.argmax(gaps))
        if gaps[k] < 0.8:
            return                      # no pause to cut at: drop it
        split(g[:k + 1])
        split(g[k + 1:])
    for g in groups:
        split(g)
    return out


def flat(s: str) -> str:
    return s.replace(" ", "")


def same_words(a: str, b: str) -> bool:
    return a.split() == b.split()


def one(path: str, rec: dict, weights: str, max_cer: float, spaces: bool = False) -> tuple[list, dict]:
    import clips
    import run_classic
    from data import pack
    from synth import SUMS_FRONT
    from shack_cw.corpus import load_iq
    from shack_cw.nn import SPACE, SYMBOLS, front_end, front_sums, load, tokens
    from val import lev
    net = load(weights)
    z, rate = load_iq(Path(path))
    exs, stats = [], {"lines": 0, "overs": 0, "kept": 0}
    for f0 in lines(z, rate):
        c = clips.to_channel(z, rate, f0)
        df = clips.keyed_line(c, 40.0)
        audio = clips.receive(clips.to_channel(z, rate, f0 + df))
        stats["lines"] += 1
        em = raw_emissions(net, front_end(audio, FS, 700.0, front=net.front))
        classic = run_classic.run_shack_cw(audio)
        prev_end = 0.0
        for g in overs(em):
            stats["overs"] += 1
            a = max(prev_end + 0.3, g[0][0] - LEAD_S, 0.0)
            b = g[-1][0] + TAIL_S
            prev_end = g[-1][0]
            text = "".join(SYMBOLS[s] for _, s in g).strip()
            other = "".join(ch for t, ch in classic if a <= t <= b + 1.5).strip()
            if len(flat(text)) < 4 or lev(flat(text), flat(other)) > max_cer * len(flat(text)):
                continue
            if spaces and not same_words(text, other):
                continue
            # The front end's sums, as data.py keeps synthetic ones; silence
            # (zeros, which Features reads as the floor) after a short over.
            c = front_sums(audio[int(a * FS):int(b * FS)], FS, 700.0, front=SUMS_FRONT)[:EXAMPLE_FRAMES]
            feat = pack(np.concatenate([c, np.zeros((EXAMPLE_FRAMES - len(c), c.shape[1]), c.dtype)]))
            lab = tokens(text)
            if not lab or lab[0] == SPACE:
                continue
            exs.append((feat, lab, text))
            stats["kept"] += 1
    return exs, dict(stats, rec=rec["id"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus")
    ap.add_argument("out")
    ap.add_argument("--weights", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--max-cer", type=float, default=0.0)
    ap.add_argument("--spaces", action="store_true", help="the two engines must also space the text alike")
    a = ap.parse_args(argv)
    from synth import SUMS_FRONT
    base = Path(a.corpus).expanduser()
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    recs = [json.loads(x) for x in (base / "manifest.jsonl").read_text().splitlines()]
    allx = []
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(one, str(base / r["file"]), r, a.weights, a.max_cer, a.spaces) for r in recs]
        for fu in cf.as_completed(futs):
            exs, st = fu.result()
            allx += exs
            print(json.dumps(st), flush=True)
    for k in range(0, len(allx), 1000):
        part = allx[k:k + 1000]
        labels, off = [], [0]
        for _, lab, _ in part:
            labels += lab
            off.append(len(labels))
        n = len(part)
        np.savez(out / f"shard-real-{k // 1000:05d}.npz", sums=np.stack([p[0] for p in part]),
                 front=np.array(SUMS_FRONT), labels=np.array(labels, np.int16), lab_off=np.array(off, np.int64), wpm=np.zeros(n),
                 t_first=np.zeros(n), t_last=np.zeros(n), snr=np.full(n, np.nan), kind=np.array(["real"] * n))
    (out / "texts.txt").write_text("\n".join(p[2] for p in allx) + "\n", encoding="utf-8")
    print(f"{len(allx)} real examples -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
