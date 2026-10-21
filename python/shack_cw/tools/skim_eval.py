#!/usr/bin/env python3
"""The skimmer's spot rule measured on the IQ library's RBN-labelled
recordings: which engines, which callsign check, how many decodes.

    python3 tools/skim_eval.py run OUT [--jobs 10] [--only ID ...]   # on office
    python3 tools/skim_eval.py score OUT [--scp MASTER.SCP] [--json F]

`run` puts each recording's CW segment through the real Skimmer with
engine="both" (the segment cut from a wideband file the way Radio's
filter bank cuts it: one channel at 48-200 kS/s), and logs every
candidate hit Calls makes - call, kind, track, frequency, time, engine -
before any rule judges it. The hits do not depend on the rule (a hit is
a call-shaped word in a pattern, in a DXCC entity), nor one engine's on
the other's (the tracks and channels are the same whichever engines
run), so `score` replays them through a fresh Calls for each variant:
one engine's hits alone are what that engine alone would have made.
That measures the code that ships, not a model of it, for the price of
one run.

Recordings: the CW survey's (office /srv/iq/derived/cw-survey/REPORT.md)
with RBN truth, less the test set's sources (the decoder bench: the box's
20 m skimmer capture, the Kiwi corpus, the Flex 12/17/30/60 m surveys)
and the dev set (`-cwval`): ohs-box wide and pair, Flex pair and the other
Flex surveys, Kiwi `-cwpseudo` and `-cwtrain`. Box probes (5 s) are too
short for anything to be seen twice.

Truth: an RBN spot of the call during the recording (+-60 s) within
RBN_HZ of our spot is `found`; precision is found over our valid spots
(`here`, a lower bound: the RBN hears from everywhere, we from one
antenna, and it never spots a station that is not calling CQ), and the
share of our valid calls the RBN spotted anywhere that day (`day`, the
survey's stand-in for "a real call"). A developer's tool: English output.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path("/srv/iq")
RBN_DAY = {"2026-10-12": ROOT / "derived" / "cw-decoder-bench" / "rbn-20261012.csv",
           "2026-10-13": ROOT / "derived" / "cw-survey" / "rbn" / "20261013.csv"}
RBN_HZ = 1000.0          # shack_cw.skim.bench's: RBN frequencies are several skimmers' average
BLOCK_S = 0.075
RATES = (48_000, 60_000, 96_000, 120_000, 200_000)   # what Radio's bank cuts a segment at
GUARD_HZ = 2_000.0
# IARU Region 1 CW segments (kHz), as the survey has them; Radio skims the
# band plan's CW-only rows, the first run of them.
CW_SEG = {"160m": (1810, 1838), "80m": (3500, 3570), "60m": (5351.5, 5354), "40m": (7000, 7040),
          "30m": (10100, 10140), "20m": (14000, 14070), "17m": (18068, 18095), "15m": (21000, 21070),
          "12m": (24890, 24915), "10m": (28000, 28070), "6m": (50000, 50100)}
TEST_SET = ("cwskimmer", "flex-12m-loop-survey", "flex-17m-loop-survey", "flex-30m-loop-survey",
            "flex-60m-loop-survey")


def chosen(rid: str) -> str | None:
    """The group a recording is scored in, or None if it is not used."""
    if any(k in rid for k in TEST_SET) or "cwval" in rid or "prop" in rid or "qo100" in rid:
        return None
    if "ohs-box" in rid:
        return None if ("probe" in rid or "test" in rid) else "box"
    if "flex" in rid:
        return "flex"
    if "cwpseudo" in rid or "cwtrain" in rid:
        return "kiwi"
    return None


# ------------------------------------------------------------ reading IQ
def _meta(rid):
    return json.loads((ROOT / "rec" / rid / "meta.json").read_text())


def open_iq(rid):
    """(reader(start, n), rate, centre, t0, n) without loading the file."""
    meta = _meta(rid)
    d = ROOT / "rec" / rid
    path = next(d / f["name"] for f in meta["files"] if f["name"].endswith((".sigmf-data", ".wav")))
    t0 = datetime.fromisoformat(meta["utc_start"].replace("Z", "+00:00")).timestamp()
    if path.suffix == ".wav":
        from shack_cw.corpus import load_iq
        z, rate = load_iq(path)
        return (lambda s, n: z[s:s + n]), rate, float(meta["centre_hz"]), t0, len(z), meta
    sm = json.loads(Path(str(path).replace(".sigmf-data", ".sigmf-meta")).read_text())
    dt = sm["global"]["core:datatype"]
    rate = float(sm["global"]["core:sample_rate"])
    centre = float(sm["captures"][0]["core:frequency"])
    if dt == "cf32_le":
        mm = np.memmap(path, "<c8", mode="r")
        return (lambda s, n: np.asarray(mm[s:s + n])), rate, centre, t0, len(mm), meta
    mm = np.memmap(path, "<i2", mode="r").reshape(-1, 2)

    def rd(s, n):
        b = np.asarray(mm[s:s + n], np.float32) / 32768.0
        return (b[:, 0] + 1j * b[:, 1]).astype(np.complex64)
    return rd, rate, centre, t0, len(mm), meta


def channel(reader, rate, nsamp, offset, out_rate):
    """One complex channel at out_rate centred `offset` Hz from the
    recording's centre: overlap-save, decimated in the frequency domain
    (the survey's cutter, at a segment's width rather than 8 kHz)."""
    D = int(round(rate / out_rate))
    assert abs(D * out_rate - rate) < 1, (rate, out_rate)
    m = 8192
    N = D * m
    keep_lo = 64
    L = keep_lo * D
    n = np.arange(L - 1) - (L - 2) / 2
    h = np.sinc(2 * 0.46 * out_rate / rate * n) * np.blackman(L - 1)
    H = np.fft.fft(h / h.sum(), N)
    hop = N - L
    j = np.fft.fftfreq(m, 1.0 / m).astype(np.int64)
    Hm = H[j % N]
    k = int(round(offset * N / rate))
    resid = offset - k * rate / N
    outs = []
    s = -L
    while s < nsamp:
        a, b = max(s, 0), min(s + N, nsamp)
        blk = np.zeros(N, np.complex64)
        blk[a - s:b - s] = reader(a, b - a)
        X = np.fft.fft(blk)
        y = np.fft.ifft(X[(k + j) % N] * Hm) * (m / N)
        y *= np.exp(-2j * np.pi * k * (s % N) / N)
        outs.append(y[keep_lo:keep_lo + hop // D].astype(np.complex64))
        s += hop
    c = np.concatenate(outs)[:int(nsamp // D)]
    if resid:
        c = c * np.exp(-2j * np.pi * resid * np.arange(len(c)) / out_rate).astype(np.complex64)
    return c


def segment_stream(rid):
    """(z, rate, centre, lo, hi, t0, band) of a recording's CW segment, or
    None when the recording does not cover it."""
    reader, rate, centre, t0, n, meta = open_iq(rid)
    band = meta.get("band")
    if band not in CW_SEG:
        return None
    slo, shi = (x * 1e3 for x in CW_SEG[band])
    if rate <= 16_000:
        rate = round(rate / 200.0) * 200.0       # a Kiwi's 11999 (bench.score_kiwi)
        lo, hi = max(slo, centre - 0.42 * rate), min(shi, centre + 0.42 * rate)
        if hi - lo < 1_000:
            return None
        return reader(0, n), rate, centre, lo, hi, t0, band
    lo, hi = max(slo, centre - 0.45 * rate + 3_000), min(shi, centre + 0.45 * rate - 3_000)
    if hi - lo < 1_000:
        return None
    need = (hi - lo + 2 * GUARD_HZ) / 0.84
    out = next((r for r in RATES if r >= need and abs(rate / r - round(rate / r)) < 1e-9), None)
    if out is None or out >= rate:
        return reader(0, n), rate, centre, lo, hi, t0, band
    mid = 0.5 * (lo + hi)
    return channel(reader, rate, n, mid - centre, out), float(out), mid, lo, hi, t0, band


# ------------------------------------------------------------------- run
def country_lookup():
    from shack_cw.skim.bench import bandplan_country
    c = bandplan_country()
    if c is None:
        raise SystemExit("needs shack_bandplan (cty.dat) on the path")
    return c


def run_one(job):
    rid, out_dir, engine, channels = job
    from shack_cw.skim import Skimmer
    t_start = time.time()
    got = segment_stream(rid)
    if got is None:
        return rid, "no segment"
    z, rate, centre, lo, hi, t0, band = got
    sk = Skimmer(rate, lo, hi, centre, max_channels=channels, country=country_lookup(), band=band, engine=engine)
    hits = []
    orig = sk.calls._hit

    def logged(call, kind, track, f, t, wpm, snr, band_, info, engine_=None):
        hits.append([call, kind, track, round(f, 1), round(t, 2), wpm, None if snr is None else round(snr, 1),
                     band_, info, engine_])
        return orig(call, kind, track, f, t, wpm, snr, band_, info, engine_)
    sk.calls._hit = logged
    blk = int(round(BLOCK_S * rate))
    c0 = time.process_time()
    for i in range(0, len(z), blk):
        sk.feed(z[i:i + blk], t=t0 + i / rate)
    cpu = time.process_time() - c0
    secs = len(z) / rate
    texts = {e: {str(k): v.text for k, v in d.items()} for e, d in sk.pool.texts_by.items()}
    res = {"id": rid, "band": band, "rate": rate, "centre": centre, "lo": lo, "hi": hi, "t0": t0,
           "t1": t0 + secs, "seconds": secs, "cpu_pct": 100 * cpu / secs, "engine": engine, "hits": hits,
           "wall_s": time.time() - t_start, "tracks_end": sk.signals}
    (Path(out_dir) / f"{rid}.json").write_text(json.dumps(res))
    return rid, f"{len(hits)} hits, cpu {res['cpu_pct']:.0f} %, {secs:.0f} s"


def run(out: Path, jobs: int, only, engine: str, channels: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rids = sorted(p.name for p in (ROOT / "rec").iterdir() if chosen(p.name))
    if only:
        rids = [r for r in rids if any(o in r for o in only)]
    todo = [(r, str(out), engine, channels) for r in rids if not (out / f"{r}.json").is_file()]
    print(f"{len(todo)} recordings to run ({len(rids)} chosen)", flush=True)
    with cf.ProcessPoolExecutor(jobs) as ex:
        for rid, msg in ex.map(run_one, todo):
            print(f"{rid}: {msg}", flush=True)


# ----------------------------------------------------------------- truth
_RBN_TXT = re.compile(r"^(\S+Z)\s+DX de (\S+?):?\s+([\d.]+)\s+(\S+)\s+(\S+)\s+(-?\d+) dB\s+(\d+) (?:WPM|BPS)")


def read_txt(path):
    out = []
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RBN_TXT.match(ln)
        if m and m.group(5) == "CW":
            t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            out.append({"t": t, "hz": float(m.group(3)) * 1e3, "call": m.group(4)})
    return out


def read_csv(path):
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if r.get("tx_mode") == "CW":
                t = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                out.append({"t": t, "hz": float(r["freq"]) * 1e3, "call": r["dx"]})
    return out


_DAY: dict = {}


def truth(rid, t0):
    """(spots near the recording, every call the RBN spotted that day)."""
    meta = _meta(rid)
    own = []
    for g in meta.get("ground_truth") or []:
        p = ROOT / g
        if p.is_file() and p.suffix == ".txt":
            own += read_txt(p)
    day = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d")
    if day not in _DAY:
        _DAY[day] = read_csv(RBN_DAY[day]) if day in RBN_DAY and RBN_DAY[day].is_file() else []
    spots = own + _DAY[day]
    return spots, {r["call"] for r in spots}


# ----------------------------------------------------------------- score
VARIANTS = {
    # name: (engines, use the list, SEEN, AGREE_S, UNLISTED_SEEN)
    "classic, main's calls.py (0.1.21-beta.1)": ("old", None, 0, 0, 0),
    "classic, shape, seen 2 (0.1.21-beta.1)": (("classic",), False, 2, 0.0, 2),
    "nn, shape, seen 2": (("nn",), False, 2, 0.0, 2),
    "both, shape, seen 2": (("nn", "classic"), False, 2, 0.0, 2),
    "both, shape, seen 2, agree 10 s = 1": (("nn", "classic"), False, 2, 10.0, 2),
    "both, SCP or shape, seen 2": (("nn", "classic"), True, 2, 0.0, 2),
    "both, SCP or shape x3": (("nn", "classic"), True, 2, 0.0, 3),
    "both, shape, seen 3 (no list)": (("nn", "classic"), False, 3, 0.0, 3),
    "both, SCP or shape x4": (("nn", "classic"), True, 2, 0.0, 4),
    "both, SCP only, seen 2": (("nn", "classic"), True, 2, 0.0, None),
    "both, SCP or shape x3, agree 10 s": (("nn", "classic"), True, 2, 10.0, 3),
    "nn, SCP or shape x3": (("nn",), True, 2, 0.0, 3),
    "classic, SCP or shape x3": (("classic",), True, 2, 0.0, 3),
    "both, seen 1 (no rule)": (("nn", "classic"), False, 1, 0.0, 1),
}


_OLD = None


def old_calls(path):
    """calls.py as it was in 0.1.21-beta.1 (ohs-libs main 7f041af), loaded
    from a file beside the new one, so "before" is exactly what shipped."""
    global _OLD
    if _OLD is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("shack_cw.skim.calls_old", path,
                                                      submodule_search_locations=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "shack_cw.skim"
        sys.modules[spec.name] = mod          # dataclasses look their module up
        spec.loader.exec_module(mod)
        _OLD = mod
    return _OLD


def replay_old(res, path):
    C = old_calls(path)
    c = C.Calls(country=None, history=None)
    for call, kind, track, f, t, wpm, snr, band, info, engine in res["hits"]:
        if engine == "classic":
            c._hit(call, kind, track, f, t, wpm, snr, band, info)
    return c.list(res["t1"])


def replay(res, engines, scp, seen, agree, unlisted):
    if engines == "old":
        return replay_old(res, scp)
    from shack_cw.skim import calls as C
    saved = (C.SEEN, C.AGREE_S, C.UNLISTED_SEEN)
    C.SEEN, C.AGREE_S, C.UNLISTED_SEEN = seen, agree, unlisted
    try:
        hist = (lambda call: call in scp or C.home_of(call) in scp) if scp is not None else None
        c = C.Calls(country=None, history=hist)
        for call, kind, track, f, t, wpm, snr, band, info, engine in res["hits"]:
            if engine in engines:
                c._hit(call, kind, track, f, t, wpm, snr, band, info, engine)
        return c.list(res["t1"])
    finally:
        C.SEEN, C.AGREE_S, C.UNLISTED_SEEN = saved


def score(out: Path, scp_path, as_json, old_path=None) -> None:
    from shack_cw.skim.scp import Scp
    scp = Scp([scp_path]).calls if scp_path else None
    results = [json.loads(p.read_text()) for p in sorted(out.glob("*.json")) if p.name[0].isdigit()]
    table = {}
    for name, (engines, use_list, seen, agree, unlisted) in VARIANTS.items():
        if (use_list and scp is None) or (engines == "old" and not old_path):
            continue
        per_group = {}
        for res in results:
            g = chosen(res["id"])
            spots, day_calls = truth(res["id"], res["t0"])
            near = [r for r in spots if res["t0"] - 60 <= r["t"] <= res["t1"] + 60
                    and res["lo"] - RBN_HZ <= r["hz"] <= res["hi"] + RBN_HZ]
            got = replay(res, engines, old_path if engines == "old" else scp if use_list else None, seen, agree,
                         unlisted)
            valid = {s["call"]: s for s in got if s["status"] in ("valid", "qsy")}
            found = {c for c, s in valid.items() if any(r["call"] == c and abs(r["hz"] - s["f"]) <= RBN_HZ
                                                         for r in near)}
            rbn_here = {r["call"] for r in near if res["lo"] <= r["hz"] <= res["hi"]}
            acc = per_group.setdefault(g, {"valid": 0, "found": 0, "in_day": 0, "rbn_calls": 0, "secs": 0.0,
                                           "unknown": []})
            acc["valid"] += len(valid)
            acc["found"] += len(found)
            acc["in_day"] += sum(1 for c in valid if c in day_calls)
            acc["rbn_calls"] += len(rbn_here)
            acc["secs"] += res["seconds"]
            acc["unknown"] += [c for c in valid if c not in day_calls]
        allg = {k: sum(v[k] for v in per_group.values()) for k in ("valid", "found", "in_day", "rbn_calls", "secs")}
        per_group["all"] = dict(allg, unknown=[])
        table[name] = per_group
    groups = ["box", "flex", "kiwi", "all"]
    print(f"{len(results)} recordings, {sum(r['seconds'] for r in results) / 3600:.2f} h; "
          f"SCP {'%d calls' % len(scp) if scp else 'none'}")
    print("variant".ljust(42) + "".join(f"{g:>26}" for g in groups))
    print(" " * 42 + "".join(f"{'found/RBN  valid  p.here p.day':>26}" for _ in groups))
    for name, pg in table.items():
        row = name.ljust(42)
        for g in groups:
            a = pg.get(g)
            if not a:
                row += " " * 26
                continue
            ph = a["found"] / a["valid"] if a["valid"] else 0
            pd = a["in_day"] / a["valid"] if a["valid"] else 0
            row += f"{a['found']:>5}/{a['rbn_calls']:<4} {a['valid']:>5}  {ph:5.0%}  {pd:5.0%}"
        print(row)
    cpu = [r["cpu_pct"] for r in results]
    print(f"cpu (both engines, this machine): median {np.median(cpu):.0f} % of a core per recording")
    if as_json:
        Path(as_json).write_text(json.dumps(table, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("run")
    a.add_argument("out")
    a.add_argument("--jobs", type=int, default=10)
    a.add_argument("--only", nargs="*")
    a.add_argument("--engine", default="both")
    a.add_argument("--channels", type=int, default=64)
    b = sub.add_parser("score")
    b.add_argument("out")
    b.add_argument("--scp")
    b.add_argument("--json")
    b.add_argument("--old-calls", help="0.1.21-beta.1's calls.py, for the 'before' row")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        run(Path(args.out), args.jobs, args.only, args.engine, args.channels)
    else:
        score(Path(args.out), args.scp, args.json, args.old_calls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
