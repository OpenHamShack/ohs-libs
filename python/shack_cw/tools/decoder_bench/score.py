#!/usr/bin/env python3
"""Score every decoder's results on the bench's clips, side by side.

    python3 tools/decoder_bench/score.py CLIPS_DIR [--json OUT] [--show call|beacon]

Per decoder (CLIPS_DIR/results/<decoder>/):

* calls: the RBN call in the decoded text. `word` counts it only as a word
  of its own (what the skimmer's matcher needs, after calls.segment()
  re-spaces); `any` with the spaces taken out (the letters were right,
  the spacing aside). Split by band and by SNR in 2.5 kHz.
* beacons: per NCDXF slot with a beacon heard - the call copied whole
  (in the slot's text with spaces out), and the character error rate of
  the best-matching stretch of that text (the slot also holds the four
  1 s dashes, which one decoder prints as T and another drops; matching
  the call where it best fits scores the call, not that). A slot's text
  is what the decoder printed from 0.3 s before the slot to 0.5 s after
  it: classic decoders print a letter after its gap, neural ones at the
  letter itself.
* noise: characters (not spaces) a minute on the quiet targets, and on
  the beacon slots where nothing is heard.
* CPU: decoder CPU per second of audio, one core, as % of a core.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np

SNR_BUCKETS = [(-99, -5, "<-5"), (-5, 5, "-5..5"), (5, 15, "5..15"), (15, 99, ">=15")]
ORDER = ["shack_cw", "shack_cw_nn", "fldigi", "ggmorse", "deepfist", "deepcw", "morseformer"]


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def infix_cer(want: str, text: str) -> float:
    """Edit distance from `want` to the best-matching substring of `text`
    (free start and end in text), over len(want)."""
    prev = [0] * (len(text) + 1)
    for i, cw in enumerate(want, 1):
        cur = [i]
        for j, ct in enumerate(text, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cw != ct)))
        prev = cur
    return min(prev) / max(1, len(want))


def bucket(snr) -> str:
    if snr is None:
        return "?"
    for lo, hi, name in SNR_BUCKETS:
        if lo <= snr < hi:
            return name
    return "?"


def text_of(ev, a=-1e9, b=1e9) -> str:
    return "".join(c for t, c in ev if a <= t < b)


def score(d: Path) -> dict:
    clips = json.loads((d / "clips.json").read_text(encoding="utf-8"))
    # The known decoders in their order, then any other results folder
    # (a run of other weights, say), by name.
    decs = [x for x in ORDER if (d / "results" / x).is_dir()]
    decs += sorted(p.name for p in (d / "results").iterdir() if p.is_dir() and p.name not in ORDER)
    res: dict = {}
    for dec in decs:
        r = {"calls": [], "slots": [], "noise_chars": 0, "noise_s": 0.0, "gap_chars": 0, "gap_s": 0.0,
             "cpu": 0.0, "secs": 0.0, "missing": 0}
        for c in clips:
            p = d / "results" / dec / f"{c['id']}.json"
            if not p.is_file():
                r["missing"] += 1
                continue
            out = json.loads(p.read_text(encoding="utf-8"))
            ev = out["events"]
            r["cpu"] += out["cpu"]
            r["secs"] += out["seconds"]
            if c["kind"] == "call":
                txt = text_of(ev).upper()
                # The home call: RBN spots "G0MBA/B" and "DJ4MZ/P"; a
                # decoder that drops the "/" or the suffix still copied
                # the call, and the matcher keys on the home call too.
                home = max(c["call"].split("/"), key=len)
                r["calls"].append({"id": c["id"], "band": c["band"], "source": c["source"], "call": home,
                                   "snr": c["snr_2500"], "word": any(home in w.split("/") for w in txt.split()),
                                   "any": home in txt.replace(" ", ""), "text": txt})
            elif c["kind"] == "noise":
                r["noise_chars"] += len(text_of(ev).replace(" ", ""))
                r["noise_s"] += out["seconds"]
            elif c["kind"] == "beacon":
                for s in c["slots"]:
                    t = text_of(ev, s["t0"] - 0.3, s["t1"] + 0.5).upper()
                    if s["expect"] is None:
                        r["gap_chars"] += len(t.replace(" ", ""))
                        r["gap_s"] += s["t1"] - s["t0"]
                    else:
                        flat = t.replace(" ", "")
                        r["slots"].append({"id": c["id"], "band": c["band"], "expect": s["expect"], "snr": s["snr"],
                                           "copied": s["expect"] in flat, "cer": infix_cer(s["expect"], flat),
                                           "text": t})
        res[dec] = r
    return res


def summarise(res: dict) -> dict:
    out = {}
    for dec, r in res.items():
        calls, slots = r["calls"], r["slots"]
        s = {"calls": len(calls), "word": sum(x["word"] for x in calls), "any": sum(x["any"] for x in calls),
             "by_source": {}, "by_band": {}, "by_snr": {},
             "slots": len(slots), "copied": sum(x["copied"] for x in slots),
             "beacon_cer": float(np.mean([x["cer"] for x in slots])) if slots else None,
             "beacon_by_snr": {},
             "noise_cpm": 60 * r["noise_chars"] / r["noise_s"] if r["noise_s"] else None,
             "gap_cpm": 60 * r["gap_chars"] / r["gap_s"] if r["gap_s"] else None,
             "cpu_ms_per_s": 1000 * r["cpu"] / r["secs"] if r["secs"] else None,
             "missing": r["missing"]}
        for key, fn in (("by_source", lambda x: x["source"]), ("by_band", lambda x: x["band"]),
                        ("by_snr", lambda x: bucket(x["snr"]))):
            g = defaultdict(list)
            for x in calls:
                g[fn(x)].append(x)
            s[key] = {k: (sum(x["word"] for x in v), sum(x["any"] for x in v), len(v)) for k, v in sorted(g.items())}
        g = defaultdict(list)
        for x in slots:
            g[bucket(x["snr"])].append(x)
        s["beacon_by_snr"] = {k: (sum(x["copied"] for x in v), float(np.mean([x["cer"] for x in v])), len(v))
                              for k, v in sorted(g.items())}
        out[dec] = s
    return out


def print_tables(summ: dict) -> None:
    decs = list(summ)
    w = 13
    print("\nRBN calls in the text (as a word / anywhere with spaces out) of n")
    keys = sorted({k for s in summ.values() for k in s["by_band"]}, key=lambda b: int(b.rstrip("m")))
    print(f"{'':14s}" + "".join(f"{d:>{w}s}" for d in decs))
    for k in keys:
        n = summ[decs[0]]["by_band"].get(k, (0, 0, 0))[2]
        print(f"{k + f' (n={n})':14s}" + "".join(f"{'%d/%d' % summ[d]['by_band'].get(k, (0, 0, 0))[:2]:>{w}s}" for d in decs))
    for _, _, b in SNR_BUCKETS:
        n = summ[decs[0]]["by_snr"].get(b, (0, 0, 0))[2]
        print(f"{'snr ' + b + f' ({n})':14s}" + "".join(f"{'%d/%d' % summ[d]['by_snr'].get(b, (0, 0, 0))[:2]:>{w}s}" for d in decs))
    for src in sorted({k for s in summ.values() for k in s["by_source"]}):
        n = summ[decs[0]]["by_source"].get(src, (0, 0, 0))[2]
        print(f"{src + f' ({n})':14s}" + "".join(f"{'%d/%d' % summ[d]['by_source'].get(src, (0, 0, 0))[:2]:>{w}s}" for d in decs))
    print(f"{'all (%d)' % summ[decs[0]]['calls']:14s}" + "".join(f"{'%d/%d' % (summ[d]['word'], summ[d]['any']):>{w}s}" for d in decs))
    print("\nNCDXF beacon slots: call copied whole / mean CER of the call")
    for _, _, b in SNR_BUCKETS:
        n = summ[decs[0]]["beacon_by_snr"].get(b, (0, 0, 0))[2]
        if n:
            print(f"{'snr ' + b + f' ({n})':14s}" + "".join(
                f"{'%d %3.0f%%' % (summ[d]['beacon_by_snr'][b][0], 100 * summ[d]['beacon_by_snr'][b][1]):>{w}s}" for d in decs))
    print(f"{'all (%d)' % summ[decs[0]]['slots']:14s}" + "".join(
        f"{'%d %3.0f%%' % (summ[d]['copied'], 100 * (summ[d]['beacon_cer'] or 0)):>{w}s}" for d in decs))
    print("\nNoise: characters a minute")
    print(f"{'quiet targets':14s}" + "".join(f"{summ[d]['noise_cpm'] or 0:>{w}.1f}" for d in decs))
    print(f"{'beacon gaps':14s}" + "".join(f"{summ[d]['gap_cpm'] or 0:>{w}.1f}" for d in decs))
    print("\nCPU, one core (this host): ms per second of audio, % of a core")
    print(f"{'':14s}" + "".join(f"{'%.1f  %.2f%%' % (summ[d]['cpu_ms_per_s'], summ[d]['cpu_ms_per_s'] / 10):>{w}s}" for d in decs))
    miss = {d: s["missing"] for d, s in summ.items() if s["missing"]}
    if miss:
        print("\nclips without results:", miss)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clips")
    ap.add_argument("--json")
    ap.add_argument("--show", choices=["call", "beacon"])
    a = ap.parse_args(argv)
    d = Path(os.path.expanduser(a.clips))
    res = score(d)
    summ = summarise(res)
    print_tables(summ)
    if a.show == "call":
        decs = list(res)
        for i, c in enumerate(res[decs[0]]["calls"]):
            print(f"\n{c['id']} {c['call']} snr {c['snr']}")
            for dcd in decs:
                x = res[dcd]["calls"][i]
                mark = "W" if x["word"] else "a" if x["any"] else "-"
                t = x["text"]
                # Around the call where it was copied, else the start.
                k = max(0, t.find(c["call"][:3]) - 40) if x["any"] else 0
                print(f"  {dcd:12s} {mark} {t[k:k + 110]!r}")
    if a.show == "beacon":
        decs = list(res)
        for i, s in enumerate(res[decs[0]]["slots"]):
            print(f"{s['id'][:28]:28s} {s['expect']:7s} {s['snr']!s:6s} " +
                  " | ".join(f"{res[dcd]['slots'][i]['text'][:16]:16s}" for dcd in decs))
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": summ, "results": res}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
