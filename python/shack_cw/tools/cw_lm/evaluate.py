#!/usr/bin/env python3
"""The language model's search against the greedy reading, on the
network's kept outputs (dump.py), at several settings.

    python3 tools/cw_lm/evaluate.py dev CACHE --lm LM.npz --cfg NAME:k=v,... [...] [--json OUT]
    python3 tools/cw_lm/evaluate.py survey CACHE --lm-fold0 LM0.npz --lm-fold1 LM1.npz --rbn CSV ... --cfg ...

A setting is `name:lm=0.5,bonus=0,beam=8,known=2,space=1` (lm 0 is the
greedy reading the engine ships; space is Bank.space_bonus), and
optionally lag_s, prune and margin (nn.lm.Search's). Each item is
replayed through Bank._read in 8-output chunks (an app's 150 ms gather),
so the search and the squelch are the live code. Characters are timed at
the output they were heard at, not when they came out, so a slower
search is not scored down for printing late; how late it printed is
reported apart (latency).

dev: decoder_bench/score.py's numbers on the realval dev set (RBN calls
as a word / anywhere, NCDXF CER, characters a minute in beacon gaps).
The dev LM must hold no text from the dev set's recordings (build.py
--skip-group kiwi-cwval).

survey: per held-out survey line (dump.py), with the LM that did not see
its fold's text (build.py --hold-fold):
* calls: each RBN call of the line in the line's text, as a word, as a
  word after the skimmer's re-spacing (calls.segment), anywhere with
  spaces out;
* windows: the 15 s windows where an RBN call of the line is in the
  window's text (the survey's 902);
* agreed: on windows where both engines agreed, the character error rate
  against the classic engine's text (the greedy network's own is the
  floor, ~5 %: what a setting breaks of text already read right);
* calls printed: call-shaped words (calls.shape_ok) an hour, how many of
  them the RBN spotted that day anywhere (--rbn), and how many are on
  the LM's own list - in `weak` windows (under -5 dB or not Morse-like)
  those are the LM's inventions.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import concurrent.futures as cf  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from collections import defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "decoder_bench"))
sys.path.insert(0, str(HERE))

CHUNK = 8


def parse_cfg(s: str) -> tuple[str, dict]:
    name, _, kv = s.partition(":")
    cfg = {"lm": 0.0, "bonus": 0.0, "beam": 8, "known": 2.0, "space": 1.0}
    for part in filter(None, kv.split(",")):
        k, v = part.split("=")
        cfg[k] = float(v)
    return name, cfg


def replay(cache: dict, cfg: dict, lm_table: str | None) -> dict:
    """One item through Bank._read at one setting: [(seconds, char)], CPU
    seconds in _read, and how late each character came out (seconds)."""
    from shack_cw.nn import Bank
    b = Bank(6.0)
    b.space_bonus = cfg["space"]
    b.lm, b.lm_table, b.lm_bonus = cfg["lm"], lm_table, cfg["bonus"]
    b.lm_beam, b.lm_known_call = int(cfg["beam"]), cfg["known"]
    b.lm_opts = {k: cfg[k] for k in ("lag_s", "prune", "margin") if k in cfg}
    b.trace = []
    b._sync([0])
    logp, wpm, opened = cache["logp"], cache["wpm"], cache["opened"]
    out_s = float(cache.get("out_s", 0.02))
    late = []
    cpu = 0.0
    for i in range(0, len(logp), CHUNK):
        lp = logp[None, i:i + CHUNK].astype(np.float32)
        n0 = len(b.trace)
        c0 = time.process_time()
        b._read(lp, wpm[None, i:i + CHUNK].astype(np.float32), opened[i:i + CHUNK, None], {0: []})
        cpu += time.process_time() - c0
        end = i + lp.shape[1]
        late += [(end - t) * out_s for _, t, _ in b.trace[n0:]]
    ev = [((t + 1) * out_s, ch) for _, t, ch in b.trace]
    return {"events": ev, "cpu": cpu, "seconds": len(logp) * out_s, "late": late}


def load_cache(p: Path) -> dict:
    z = np.load(p)
    return {k: z[k] for k in z.files}


# --------------------------------------------------------------- dev set
def _dev_item(job):
    path, cfgs, lm_table = job
    cache = load_cache(Path(path))
    return Path(path).stem, {name: replay(cache, cfg, lm_table) for name, cfg in cfgs}


def dev(cache: Path, cfgs, lm_table: str, jobs: int) -> dict:
    import score as bench_score
    clips = json.loads((cache / "clips.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="cwlm-dev-", dir=Path.home() / "build"))
    (tmp / "clips.json").write_text(json.dumps(clips))
    late, cpu = defaultdict(list), defaultdict(float)
    with cf.ProcessPoolExecutor(jobs) as ex:
        for cid, res in ex.map(_dev_item, [(str(cache / f"{c['id']}.npz"), cfgs, lm_table) for c in clips]):
            for name, r in res.items():
                d = tmp / "results" / name
                d.mkdir(parents=True, exist_ok=True)
                late[name] += r.pop("late")
                (d / f"{cid}.json").write_text(json.dumps(r))
    summ = bench_score.summarise(bench_score.score(tmp))
    bench_score.print_tables(summ)
    for name, _ in cfgs:
        s = summ[name]
        s["late_p50"], s["late_p95"] = _pct(late[name])
    print("\nlate (s) p50/p95:", {n: (summ[n]["late_p50"], summ[n]["late_p95"]) for n, _ in cfgs})
    import shutil
    shutil.rmtree(tmp)
    return summ


def _pct(x):
    if not x:
        return None, None
    return round(float(np.percentile(x, 50)), 2), round(float(np.percentile(x, 95)), 2)


# --------------------------------------------------------------- survey
def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def flat(s: str) -> str:
    return s.replace(" ", "")


def _home(call: str) -> str:
    return max(call.split("/"), key=len)


def survey_line(ln: dict, r: dict, day_calls: set, lm_calls: frozenset) -> dict:
    from shack_cw.skim.calls import segment, shape_ok
    ev = r["events"]
    txt = "".join(c for _, c in ev)
    words = txt.split()
    seg = segment(words)
    calls = [_home(c) for c in ln["rbn_calls"]]
    res = {"calls": len(calls),
           "word": sum(any(c in w.split("/") for w in words) for c in calls),
           "seg": sum(any(c in w.split("/") for w in seg) for c in calls),
           "any": sum(c in flat(txt) for c in calls),
           "found": [c for c in calls if c in flat(txt)],
           # The same for the calls on the LM's own list: where the model
           # can know the call, and where it can only know its shape.
           "calls_on": sum(c in lm_calls for c in calls),
           "word_on": sum(c in lm_calls and any(c in w.split("/") for w in words) for c in calls),
           "seg_on": sum(c in lm_calls and any(c in w.split("/") for w in seg) for c in calls),
           "any_on": sum(c in lm_calls and c in flat(txt) for c in calls),
           "cpu": r["cpu"], "seconds": r["seconds"], "late": r["late"],
           "win_any": 0, "win_word": 0, "win_base": 0, "agree_err": 0, "agree_len": 0,
           "printed": 0, "printed_day": 0, "weak_printed": 0, "weak_on_list": 0, "weak_s": 0.0}
    for w in ln["windows"]:
        if w["cls"] in (None, "idle"):
            continue
        wt = "".join(c for t, c in ev if w["a"] <= t < w["b"])
        if calls:
            res["win_base"] += bool(w.get("rbn_hit"))
            res["win_any"] += any(c in flat(wt) for c in calls)
            res["win_word"] += any(any(c in x.split("/") for x in wt.split()) for c in calls)
        if w["cls"] == "agree":
            ref = flat(w["classic"])
            res["agree_err"] += lev(flat(wt), ref)
            res["agree_len"] += len(ref)
        shaped = [x for x in segment(wt.split()) if len(x) >= 3 and shape_ok(x)]
        res["printed"] += len(shaped)
        res["printed_day"] += sum(_home(x) in day_calls for x in shaped)
        if w["cls"] == "weak":
            res["weak_s"] += w["b"] - w["a"]
            res["weak_printed"] += len(shaped)
            res["weak_on_list"] += sum(_home(x) in lm_calls for x in shaped)
    return res


def _survey_item(job):
    path, ln, cfgs, lm_table, day_calls = job
    from shack_cw.nn.lm import load as load_lm
    cache = load_cache(Path(path))
    lm_calls = load_lm(lm_table).calls
    out = {}
    for name, cfg in cfgs:
        r = replay(cache, cfg, lm_table)
        out[name] = survey_line(ln, r, day_calls, lm_calls)
    return ln["id"], out


def read_rbn_calls(paths: list[str]) -> set:
    calls = set()
    for p in paths:
        with open(p, newline="", encoding="latin-1") as f:
            for row in csv.DictReader(f):
                c = row.get("dx") or row.get("call") or row.get("dx_call")
                if c:
                    calls.add(_home(c.upper()))
    return calls


def survey(cache: Path, cfgs, lm_tables: dict, rbn: list[str], jobs: int) -> dict:
    from build import fold
    lines = json.loads((cache / "index.json").read_text())
    day = read_rbn_calls(rbn)
    tot: dict = {name: defaultdict(float) for name, _ in cfgs}
    late = defaultdict(list)
    by_group: dict = {name: defaultdict(lambda: [0, 0, 0, 0]) for name, _ in cfgs}
    found: dict = {name: {} for name, _ in cfgs}
    jobs_ = [(str(cache / f"{ln['id']}.npz"), ln, cfgs, lm_tables[fold(ln["rec"])], day) for ln in lines
             if (cache / f"{ln['id']}.npz").is_file()]
    with cf.ProcessPoolExecutor(jobs) as ex:
        for lid, res in ex.map(_survey_item, jobs_, chunksize=2):
            for name, r in res.items():
                late[name] += r.pop("late")
                found[name][lid] = r.pop("found")
                g = next(x["group"] for x in lines if x["id"] == lid)
                bg = by_group[name][g]
                bg[0] += r["calls"]
                bg[1] += r["word"]
                bg[2] += r["seg"]
                bg[3] += r["any"]
                for k, v in r.items():
                    tot[name][k] += v
    summ = {}
    for name, _ in cfgs:
        t = tot[name]
        h = t["seconds"] / 3600
        summ[name] = {
            "lines": len(jobs_), "hours": round(h, 2), "calls": int(t["calls"]), "word": int(t["word"]),
            "seg": int(t["seg"]), "any": int(t["any"]),
            "on_list c/w/s/a": "/".join(str(int(t[k])) for k in ("calls_on", "word_on", "seg_on", "any_on")),
            "windows_base": int(t["win_base"]), "windows_any": int(t["win_any"]), "windows_word": int(t["win_word"]),
            "agree_cer": round(t["agree_err"] / max(1, t["agree_len"]), 4),
            "printed_per_h": round(t["printed"] / h, 1),
            "printed_day_share": round(t["printed_day"] / max(1, t["printed"]), 3),
            "weak_printed_per_h": round(t["weak_printed"] / max(1e-9, t["weak_s"] / 3600), 1),
            "weak_on_list_per_h": round(t["weak_on_list"] / max(1e-9, t["weak_s"] / 3600), 1),
            "cpu_pct": round(100 * t["cpu"] / t["seconds"], 4),
            "late": _pct(late[name]),
            "by_group": dict(by_group[name]),
        }
    names = [n for n, _ in cfgs]
    keys = ["calls", "word", "seg", "any", "on_list c/w/s/a", "windows_base", "windows_any", "windows_word", "agree_cer",
            "printed_per_h", "printed_day_share", "weak_printed_per_h", "weak_on_list_per_h", "cpu_pct", "late"]
    w = max(12, max(len(n) for n in names) + 2)
    print(f"{'':22s}" + "".join(f"{n:>{w}s}" for n in names))
    for k in keys:
        print(f"{k:22s}" + "".join(f"{str(summ[n][k]):>{w}s}" for n in names))
    for g in sorted(by_group[names[0]]):
        print(f"{g + ' c/w/s/a':22s}" + "".join(f"{'/'.join(str(v) for v in by_group[n][g]):>{w}s}" for n in names))
    # Which calls each setting gains and loses against the first.
    base = names[0]
    for n in names[1:]:
        gain = sorted(c for lid in found[n] for c in set(found[n][lid]) - set(found[base].get(lid, [])))
        lost = sorted(c for lid in found[n] for c in set(found[base].get(lid, [])) - set(found[n][lid]))
        summ[n]["gained"], summ[n]["lost"] = gain, lost
        print(f"{n}: +{len(gain)} {' '.join(gain)} | -{len(lost)} {' '.join(lost)}")
    return summ


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("dev")
    a1.add_argument("cache")
    a1.add_argument("--lm", required=True)
    a2 = sub.add_parser("survey")
    a2.add_argument("cache")
    a2.add_argument("--lm-fold0", required=True, help="the LM built with --hold-fold 0")
    a2.add_argument("--lm-fold1", required=True, help="the LM built with --hold-fold 1")
    a2.add_argument("--rbn", nargs="+", required=True, help="the survey days' RBN archives (CSV)")
    for p in (a1, a2):
        p.add_argument("--cfg", nargs="+", required=True)
        p.add_argument("--jobs", type=int, default=12)
        p.add_argument("--json")
    a = ap.parse_args(argv)
    os.nice(10)
    cfgs = [parse_cfg(s) for s in a.cfg]
    if a.cmd == "dev":
        summ = dev(Path(a.cache), cfgs, a.lm, a.jobs)
    else:
        summ = survey(Path(a.cache), cfgs, {0: a.lm_fold0, 1: a.lm_fold1}, a.rbn, a.jobs)
    if a.json:
        Path(a.json).write_text(json.dumps(summ, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
