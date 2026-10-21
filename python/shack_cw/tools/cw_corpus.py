#!/usr/bin/env python3
"""Record the CW corpus: real HF IQ from public KiwiSDRs, for shack_cw.corpus.

    tools/cw_corpus.py record PLAN.json [--dir ~/build/cw-corpus] [--jobs 4]
    tools/cw_corpus.py annotate MANIFEST.json       # what is known in each, from RBN spots
    tools/cw_corpus.py fixture ID OFFSET START SECONDS OUT.npz
    python3 tools/rbn.py SECONDS DIR/rbn-spots.txt   # alongside recording: the RBN's spots

The corpus itself is minutes of 12 kHz IQ per band - tens of megabytes - so
it lives outside git (default ~/build/cw-corpus: iq/*.wav and
manifest.jsonl); git holds this script, the plan that made it
(tests/corpus/plan.json), the manifest's ground truth
(tests/corpus/manifest.json) and a few short clips cut from it
(tests/fixtures/, a few hundred KB), which is what the tests run.

Recording is receive-only, from other people's receivers over the
internet: no radio here is touched. KiwiSDR because it is the one public
receiver network that hands out raw IQ (kiwirecorder, jks-prv/kiwiclient,
cloned into DIR/tools on first use - a developer's tool, never shipped or
vendored), with a fixed manual gain (`gain` in the plan, AGC off) so the
recording keeps the band's own dynamics and the receiver under test
applies its own AGC, as the SDRplay in Radio does.

A plan is a list of recordings:

    {"id": "20m-ncdxf-wessex", "kind": "beacon", "band": "20m",
     "khz": 14100.0, "host": "wessex.zapto.org", "port": 8074,
     "seconds": 180, "align": 180, "gain": 55}

`khz` is the centre of the IQ (a beacon's frequency, or the middle of a
busy segment); the IQ covers 5 kHz either side of it.
`align` waits for the next multiple of that many seconds of UTC first (the
NCDXF beacons' 3-minute cycle). Each finished recording is appended to
DIR/manifest.jsonl with the UTC time its first sample was asked for.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

KIWICLIENT = "https://github.com/jks-prv/kiwiclient.git"


def kiwiclient(base: Path) -> Path:
    d = base / "tools" / "kiwiclient"
    if not (d / "kiwirecorder.py").is_file():
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", KIWICLIENT, str(d)], check=True)
    return d


def record_one(base: Path, rec: dict) -> dict | None:
    kc = kiwiclient(base)
    out = base / "iq"
    out.mkdir(parents=True, exist_ok=True)
    align = float(rec.get("align") or 0)
    if align:
        now = time.time()
        # Ask two seconds early: the connection takes about one.
        start = (int(now // align) + 1) * align - 2.0
        if start - now < 1:
            start += align
        time.sleep(start - now)
    t0 = datetime.now(timezone.utc)
    name = f"{rec['id']}-{t0:%Y%m%dT%H%M%SZ}"
    cmd = [sys.executable, "kiwirecorder.py", "-q", "-s", rec["host"], "-p", str(rec.get("port", 8073)),
           "-f", f"{float(rec['khz']):.3f}", "-m", "iq", "-L", "-5000", "-H", "5000",
           "-g", str(rec.get("gain", 55)), "--tlimit", str(rec["seconds"]),
           "-d", str(out), "--fn", name, "-u", "ohs-cw-corpus"]
    try:
        p = subprocess.run(cmd, cwd=kc, capture_output=True, text=True, timeout=rec["seconds"] + 90)
    except subprocess.TimeoutExpired:
        # A receiver that never answers (kiwirecorder retries for ever).
        print(f"{rec['id']}: {rec['host']} did not answer", file=sys.stderr)
        return None
    wav = out / f"{name}.wav"
    if not wav.is_file() or wav.stat().st_size < 100_000:
        print(f"{rec['id']}: no recording ({p.returncode}) {p.stderr[-300:]}", file=sys.stderr)
        return None
    entry = dict(rec, file=f"iq/{wav.name}", utc=t0.isoformat(timespec="seconds"),
                 source=f"KiwiSDR {rec['host']}:{rec.get('port', 8073)}")
    with open(base / "manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"{rec['id']}: {wav.name} {wav.stat().st_size // 1024} KB", flush=True)
    return entry


RBN = re.compile(r"^(\S+) DX de (\S+?):\s+([\d.]+)\s+(\S+)\s+CW\s+(-?\d+) dB\s+(\d+) WPM\s+(.*?)\s+\d{4}Z")


def rbn_spots(path: Path) -> list[dict]:
    """The Reverse Beacon Network spots tools/rbn.py captured: [{utc,
    skimmer, khz, call, db, wpm, what}], each once (captures may overlap)."""
    out, seen = [], set()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = RBN.match(line)
        if not m or line[21:] in seen:
            continue
        seen.add(line[21:])
        t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
        out.append({"utc": t, "skimmer": m.group(2), "khz": float(m.group(3)), "call": m.group(4),
                    "db": int(m.group(5)), "wpm": int(m.group(6)), "what": m.group(7).strip()})
    return out


def _category(snr: float, wpm: float | None) -> str:
    if wpm and wpm >= 30:
        return "fast"
    if wpm and wpm <= 16:
        return "slow"
    return "strong" if snr >= 15 else "weak" if snr < 5 else "medium"


def annotate(base: Path, rec: dict, spots: list[dict]) -> list[dict]:
    """What is known in one recording, as shack_cw.corpus targets: a
    beacon's slots; the RBN-spotted stations (call and speed); quiet places
    (noise); and, beside the strongest stations, Brian's 20 m case - the
    pitch 600 Hz below a real signal inside a 1500 Hz filter."""
    from shack_cw import corpus
    z, rate = corpus.load_iq(base / rec["file"])
    t0 = datetime.fromisoformat(rec["utc"]).timestamp()
    t1 = t0 + len(z) / rate
    if rec["kind"] == "beacon":
        return [corpus.beacon_slots(z, rate, rec["khz"], t0)]
    targets: list[dict] = []
    found = corpus.find_signals(z, rate)
    for sig in found:
        khz = rec["khz"] + sig["offset"] / 1000.0
        near = [s for s in spots if abs(s["khz"] - khz) <= 0.25 and t0 - 600 <= s["utc"] <= t1 + 600]
        if not near or sig["snr"] < -3:
            continue
        # The spot nearest in frequency, then in time; the speed is the
        # median over every skimmer that heard that call there.
        near.sort(key=lambda s: (abs(s["khz"] - khz), abs(s["utc"] - t0)))
        call = near[0]["call"]
        wpms = sorted(s["wpm"] for s in near if s["call"] == call)
        wpm = wpms[len(wpms) // 2]
        targets.append({"name": call, "offset": sig["offset"], "call": call, "wpm": wpm, "onset": sig["onset"],
                        "snr": sig["snr"], "category": _category(sig["snr"], wpm),
                        "rbn": f"{len(wpms)} spots, {near[0]['skimmer']} {near[0]['khz']}"})
        if sig["snr"] >= 15 and sum(t.get("category") == "beside-wide" for t in targets) < 2:
            targets.append({"name": f"{call}-600", "offset": sig["offset"] - 600.0, "width": 1500.0,
                            "noise": True, "category": "beside-wide"})
    # Quiet places: no line found within 250 Hz, inside the IQ's +-4 kHz.
    lines = [s["offset"] for s in found]
    quiet = [float(o) for o in np.arange(-3800.0, 3801.0, 200.0) if all(abs(o - x) > 250 for x in lines)]
    for o in quiet[::max(1, len(quiet) // 3)][:3]:
        targets.append({"name": f"quiet{o:+.0f}", "offset": o, "noise": True, "category": "noise"})
    return targets


def fixture(base: Path, rec: dict, offset: float, start: float, seconds: float, out: Path) -> None:
    """A short clip of one target as a test fixture: the IQ moved so the
    target is at 0 Hz, cut to +-750 Hz and 1500 samples a second (int16,
    about 6 KB a second), which is all a 500 Hz CW filter and the
    decoder's noise references either side of the tone ever see."""
    from shack_cw import corpus
    z, rate = corpus.load_iq(base / rec["file"])
    a = int(start * rate)
    z = z[a:a + int(seconds * rate)]
    z = z * np.exp(-2j * np.pi * offset * np.arange(len(z)) / rate)
    n = len(z)
    Z = np.fft.fft(z)
    m = int(n * 1500 / rate)
    keep = np.concatenate([Z[:m // 2], Z[-(m - m // 2):]]) * (m / n)
    y = np.fft.ifft(keep)
    peak = float(np.max(np.abs(np.concatenate([y.real, y.imag])))) or 1.0
    q = np.round(np.stack([y.real, y.imag], axis=1) / peak * 32000).astype(np.int16)
    np.savez_compressed(out, iq=q, rate=np.float64(1500.0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a plan's IQ into DIR/iq, adding to DIR/manifest.jsonl")
    r.add_argument("plan")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--only", default="")
    n = sub.add_parser("annotate", help="DIR/manifest.jsonl and DIR/rbn-spots.txt to a scoring manifest")
    n.add_argument("out")
    f = sub.add_parser("fixture", help="cut one target into a short test clip")
    f.add_argument("id")
    f.add_argument("offset", type=float)
    f.add_argument("start", type=float)
    f.add_argument("seconds", type=float)
    f.add_argument("out")
    for p in (r, n, f):
        p.add_argument("--dir", default=str(Path.home() / "build" / "cw-corpus"))
    a = ap.parse_args(argv)
    base = Path(os.path.expanduser(a.dir))
    recs = [json.loads(x) for x in (base / "manifest.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (base / "manifest.jsonl").is_file() else []
    if a.cmd == "annotate":
        spots = rbn_spots(base / "rbn-spots.txt")
        for rec in recs:
            rec["targets"] = annotate(base, rec, spots)
            print(rec["id"], rec["utc"], [t.get("name") for t in rec["targets"]], flush=True)
        Path(a.out).write_text(json.dumps(recs, indent=1) + "\n", encoding="utf-8")
        return 0
    if a.cmd == "fixture":
        rec = next(x for x in recs if x["id"] == a.id or x["file"].endswith(a.id))
        fixture(base, rec, a.offset, a.start, a.seconds, Path(a.out))
        return 0
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    if a.only:
        plan = [p for p in plan if p["id"] in a.only.split(",")]
    # One connection per receiver at a time: many public Kiwis refuse a
    # second from the same address ("No multiple connections from the same
    # IP address"), so each receiver's recordings run in turn and
    # different receivers in parallel.
    by_host: dict[str, list[dict]] = {}
    for rec in plan:
        by_host.setdefault(rec["host"], []).append(rec)

    def run(recs):
        for rec in recs:
            record_one(base, rec)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(run, by_host.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
