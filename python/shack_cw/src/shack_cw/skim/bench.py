"""Measuring the skimmer: its CPU, and its spots against ground truth.

    python3 -m shack_cw.skim.bench cpu [--channels 32,64,128] [--seconds 20]
    python3 -m shack_cw.skim.bench synth [--stations 30] [--seconds 60]
    python3 -m shack_cw.skim.bench corpus MANIFEST.json [--dir ~/build/cw-corpus] [--rbn FILE]

`cpu`: a synthetic 96 kS/s segment with as many stations as channels;
the front end alone (no decoders) and then K channels, as % of one core
of this machine (process time over audio time). CW-SKIMMER-PLAN §2.1
measured ~2 % + 0.26 % per channel on ohs-dev; §7.4 passes if the 64-row
holds within 2x.

`synth`: spot recall and precision against the stations rendered, busted
spots, frequency and speed error, time to first spot.

`corpus`: every recording of the manifest through the skimmer. Kiwi
recordings (12 kHz) are scored per target: a target with a `call` (an RBN
spot) should be spotted within TARGET_HZ, a `noise` target never. A
wideband recording (`"kind": "wideband"`, SigMF) is scored against the
RBN spots captured with it (`rbn`): which RBN calls we spotted (within
RBN_HZ), and which of our confirmed spots the RBN never made - each to be
checked by ear, since the RBN hears from everywhere and we from one
antenna. Needs a country lookup: shack_bandplan's cty.dat if installed.

A developer's tool, like shack_cw.bench: English output, not translated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import Skimmer, synth

TARGET_HZ = 100.0
RBN_HZ = 1000.0
BLOCK_S = 0.075


def bandplan_country():
    """shack_bandplan's cty.dat as a lookup, or None if it is not there."""
    try:
        from shack_bandplan import cty, data
    except ImportError:
        return None
    C = cty.load(data.bundled().cty_path)

    def country(call):
        e = C.lookup(call)
        return None if e is None else {"country": e.name, "continent": e.continent, "dxcc": e.prefix}
    return country


def run(z: np.ndarray, rate: float, *, lo: float, hi: float, centre: float = 0.0, t0: float = 0.0,
        max_channels: int = 64, country=None, band: str | None = None, squelch_db: float = 6.0,
        engine: str = "both"):
    """Feed z through a Skimmer in 75 ms blocks: (skimmer, spot items, cpu s)."""
    sk = Skimmer(rate, lo, hi, centre, max_channels=max_channels, country=country, band=band,
                 squelch_db=squelch_db, engine=engine)
    blk = int(round(BLOCK_S * rate))
    items = []
    c0 = time.process_time()
    for i in range(0, len(z), blk):
        items += sk.feed(z[i:i + blk], t=t0 + i / rate)
    return sk, items, time.process_time() - c0


# ------------------------------------------------------------------ cpu
def cpu(channels=(32, 64, 128), seconds: float = 20.0, rate: float = 96_000.0, seed: int = 0,
        engine: str = "both") -> dict:
    """{"front": % of a core, K: {"total", "per_channel", "decoding"}}."""
    n = max(channels)
    st = synth.random_band(n, 0.84 * rate - 4000, seed=seed, min_gap=min(150.0, 0.8 * rate / n))
    z = synth.render(st, seconds, rate, seed=seed)
    lim = 0.42 * rate
    out = {}
    _, _, c = run(z, rate, lo=-lim, hi=lim, max_channels=0)
    out["front"] = 100.0 * c / seconds
    for k in channels:
        sk, _, c = run(z, rate, lo=-lim, hi=lim, max_channels=k, engine=engine)
        total = 100.0 * c / seconds
        dec = max(1, sk.decoding)
        out[k] = {"total": total, "per_channel": (total - out["front"]) / dec, "decoding": sk.decoding}
    return out


# ---------------------------------------------------------------- synth
def score_synth(stations: list[synth.Station], spots: list[dict], items: list[dict]) -> dict:
    truth = {s.call: s for s in stations}
    valid = {s["call"]: s for s in spots if s["status"] in ("valid", "qsy")}
    found = [c for c in truth if c in valid]
    false = [c for c in valid if c not in truth]
    busted_valid = [c for c in false]          # a wrong call confirmed is a busted spot that got through
    ferr = [abs(valid[c]["f"] - truth[c].offset - truth[c].drift * valid[c]["last"]) for c in found]
    werr = [abs(valid[c]["wpm"] - truth[c].wpm) / truth[c].wpm for c in found if valid[c].get("wpm")]
    first = {}
    for it in items:
        if it["status"] in ("valid", "qsy") and it["call"] not in first:
            first[it["call"]] = it["last"] - truth[it["call"]].start if it["call"] in truth else None
    tfs = [v for v in first.values() if v is not None]
    return {"stations": len(truth), "recall": len(found) / max(1, len(truth)),
            "precision": (len(valid) - len(false)) / max(1, len(valid)), "false": false,
            "busted_spotted": len(busted_valid), "missed": [c for c in truth if c not in valid],
            "freq_err_median": float(np.median(ferr)) if ferr else None, "freq_err_max": max(ferr) if ferr else None,
            "wpm_err_median": float(np.median(werr)) if werr else None,
            "first_spot_median_s": float(np.median(tfs)) if tfs else None}


# --------------------------------------------------------------- corpus
_RBN = re.compile(r"^(\S+) DX de (\S+):\s+([\d.]+)\s+(\S+)\s+(\S+)\s+(-?\d+) dB\s+(\d+) (WPM|BPS)\s*(\S*)")


def read_rbn(path: Path) -> list[dict]:
    out = []
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RBN.match(ln)
        if not m or m.group(5) != "CW":
            continue
        t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        out.append({"t": t, "by": m.group(2), "hz": float(m.group(3)) * 1000.0, "call": m.group(4),
                    "snr": int(m.group(6)), "wpm": int(m.group(7)), "kind": m.group(9)})
    return out


def load_sigmf(data_path: Path) -> tuple[np.ndarray, float, float, float]:
    """(IQ complex64, rate, centre Hz, start UTC s) of a SigMF recording."""
    meta = json.loads(Path(str(data_path).replace(".sigmf-data", ".sigmf-meta")).read_text())
    g, cap = meta["global"], meta["captures"][0]
    dt = g["core:datatype"]
    if dt == "cf32_le":
        z = np.fromfile(data_path, "<c8")
    elif dt == "ci16_le":
        z = np.fromfile(data_path, "<i2").astype(np.float32).reshape(-1, 2) @ np.array([1, 1j], np.complex64) / 32768
    else:
        raise ValueError(f"cannot read {dt}")
    start = cap.get("openhamshack:utc") or datetime.fromisoformat(cap["core:datetime"].replace("Z", "+00:00")).timestamp()
    return z.astype(np.complex64), float(g["core:sample_rate"]), float(cap["core:frequency"]), float(start)


def score_wideband(rec: dict, base: Path, country, rbn_path: Path | None = None, max_channels: int = 64,
                   seconds: float | None = None, engine: str = "both") -> dict:
    z, rate, centre, t0 = load_sigmf(base / rec["file"])
    if seconds:
        z = z[:int(seconds * rate)]
    lo, hi = rec.get("lo", centre - 0.42 * rate), rec.get("hi", centre + 0.42 * rate)
    sk, items, c = run(z, rate, lo=lo, hi=hi, centre=centre, t0=t0, max_channels=max_channels,
                       country=country, band=rec.get("band"), engine=engine)
    secs = len(z) / rate
    t1 = t0 + secs
    spots = sk.calls.list(t1)
    ours = {s["call"]: s for s in spots if s["status"] in ("valid", "qsy")}
    out = {"id": rec["id"], "seconds": secs, "cpu_pct": 100 * c / secs, "signals_end": sk.signals,
           "spots_valid": len(ours), "spots_unconfirmed": sum(1 for s in spots if s["status"] == "unconfirmed"),
           "spots_busted": sum(1 for s in spots if s["status"] == "busted")}
    rbn_file = rbn_path or (base / rec["rbn"] if rec.get("rbn") else None)
    if rbn_file and Path(rbn_file).is_file():
        rbn = [r for r in read_rbn(Path(rbn_file)) if t0 - 30 <= r["t"] <= t1 + 30 and lo <= r["hz"] <= hi]
        rcalls: dict[str, list] = {}
        for r in rbn:
            rcalls.setdefault(r["call"], []).append(r)
        both = [c for c in ours if c in rcalls and any(abs(ours[c]["f"] - r["hz"]) <= RBN_HZ for r in rcalls[c])]
        # Strongest RBN reports first: the ones a single receiver should hear.
        best = {c: max(r["snr"] for r in rs) for c, rs in rcalls.items()}
        strong = [c for c in rcalls if best[c] >= 15]
        out.update({"rbn_calls": len(rcalls), "rbn_spots": len(rbn), "found": len(both),
                    "recall_vs_rbn": len(both) / max(1, len(rcalls)),
                    "recall_vs_rbn_15db": sum(1 for c in strong if c in both) / max(1, len(strong)),
                    "rbn_15db": len(strong),
                    "ours_in_rbn": len(both) / max(1, len(ours)),
                    "ours_not_in_rbn": sorted((c, round(ours[c]["f"] / 1000, 2), ours[c]["hits"], ours[c]["snr"])
                                              for c in ours if c not in both),
                    "snr_vs_rbn": [(c, ours[c]["snr"], best[c]) for c in both]})
    return out


def score_kiwi(manifest: list[dict], base: Path, country, engine: str = "both") -> dict:
    from ..corpus import load_iq
    hits = misses = false = noise_targets = in_text = 0
    rows = []
    cpu_s = secs_total = 0.0
    for rec in manifest:
        if rec.get("kind") == "wideband" or not any("call" in t or t.get("noise") for t in rec.get("targets", [])):
            continue
        path = base / rec["file"]
        if not path.is_file():
            continue
        z, rate = load_iq(path)
        # A Kiwi's header says 11999 or 12000 (shack_cw.corpus.load_iq): the
        # filter bank needs whole 5 ms frames, and 1 in 12000 is no error.
        rate = round(rate / 200.0) * 200.0
        lim = 0.42 * rate
        sk, items, c = run(z, rate, lo=-lim, hi=lim, country=country, band=rec.get("band"), engine=engine)
        cpu_s += c
        secs_total += len(z) / rate
        spots = sk.calls.list(len(z) / rate)
        ours = [s for s in spots if s["status"] in ("valid", "qsy")]
        for tg in rec["targets"]:
            off = float(tg.get("offset", 0.0))
            near = [s for s in ours if abs(s["f"] - off) <= TARGET_HZ]
            if "call" in tg:
                ok = any(s["call"] == tg["call"] for s in near)
                hits += ok
                misses += not ok
                # Whether the call is anywhere in the text of the channels
                # there (spaces taken out): the decoders' part, apart from
                # the call matcher's - the single-signal corpus scores this.
                text = "".join(sk.pool.texts[tr.id].text for tr in sk.tracker.tracks.values()
                               if abs(tr.f - off) <= TARGET_HZ and tr.id in sk.pool.texts).replace(" ", "")
                in_text += tg["call"] in text
                rows.append((rec["id"], tg["call"], ok, tg["call"] in text, [s["call"] for s in near]))
            elif tg.get("noise"):
                noise_targets += 1
                false += bool(near)
                if near:
                    rows.append((rec["id"], tg.get("name"), "FALSE", [s["call"] for s in near]))
    return {"calls_found": hits, "calls": hits + misses, "calls_in_text": in_text, "noise_targets": noise_targets,
            "noise_false": false, "rows": rows, "cpu_pct": 100 * cpu_s / max(1e-9, secs_total)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", choices=Skimmer.ENGINES, default="both",
                    help="the CW engines behind the channels (Skimmer.ENGINES)")
    sub = ap.add_subparsers(dest="what", required=True)
    a1 = sub.add_parser("cpu")
    a1.add_argument("--channels", default="32,64,128")
    a1.add_argument("--seconds", type=float, default=20.0)
    a2 = sub.add_parser("synth")
    a2.add_argument("--stations", type=int, default=30)
    a2.add_argument("--seconds", type=float, default=60.0)
    a2.add_argument("--seed", type=int, default=1)
    a3 = sub.add_parser("corpus")
    a3.add_argument("manifest")
    a3.add_argument("--dir", default=str(Path.home() / "build" / "cw-corpus"))
    a3.add_argument("--only", help="a recording id")
    a3.add_argument("--seconds", type=float, help="score only this much of a wideband recording")
    a3.add_argument("--channels", type=int, default=64)
    a3.add_argument("--json")
    a = ap.parse_args(argv)
    if a.what == "cpu":
        r = cpu(tuple(int(x) for x in a.channels.split(",")), a.seconds, engine=a.engine)
        print(f"front end (STFT, detection, tracking), no decoders: {r['front']:.1f} % of a core")
        for k, v in r.items():
            if k != "front":
                print(f"{k:4d} channels: {v['total']:.1f} % of a core ({v['decoding']} decoding;"
                      f" {v['per_channel']:.2f} % a channel)")
        return 0
    country = bandplan_country()
    if a.what == "synth":
        rate = 96_000.0
        st = synth.random_band(a.stations, 66_000.0, seed=a.seed)
        z = synth.render(st, a.seconds, rate, seed=a.seed, carriers=[(12_345.0, 25.0)], static=2.0)
        sk, items, c = run(z, rate, lo=-35_000.0, hi=35_000.0, country=country, engine=a.engine)
        r = score_synth(st, sk.calls.list(a.seconds), items)
        r["cpu_pct"] = 100 * c / a.seconds
        print(json.dumps(r, indent=1))
        return 0
    base = Path(os.path.expanduser(a.dir))
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    if a.only:
        manifest = [r for r in manifest if r["id"] == a.only]
    res = {}
    wide = [r for r in manifest if r.get("kind") == "wideband"]
    if [r for r in manifest if r.get("kind") != "wideband"]:
        res["kiwi"] = score_kiwi(manifest, base, country, engine=a.engine)
        k = res["kiwi"]
        print(f"Kiwi recordings: {k['calls_found']}/{k['calls']} RBN-spotted calls spotted"
              f" ({k['calls_in_text']} in the channels' text); "
              f"{k['noise_false']}/{k['noise_targets']} quiet targets with a spot; CPU {k['cpu_pct']:.1f} %")
        for row in k["rows"]:
            print("  ", *row)
    for rec in wide:
        r = score_wideband(rec, base, country, max_channels=a.channels, seconds=a.seconds, engine=a.engine)
        res[rec["id"]] = r
        print(json.dumps(r, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
