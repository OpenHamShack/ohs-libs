"""Capture the golden numbers the Rust SSTV decoder is held to (RADIO-SDR-PLAN
§7.1): openhamshack-nb's Python `ohscore/modes/sstv.py` Decoder run on every
synthetic fixture, before that decoder is removed.

    PYTHONPATH=<dev build> python rust/shack_sdr/tools/sstv_golden.py --nb ~/openhamshack-nb

Writes rust/shack_sdr/testdata/sstv/cases.json (each case's recipe for the
test transmitter, shack_sdr::sstv::tx, and the sha256 of the samples it
makes, so the fixtures are pinned byte for byte) and golden.json (how
sstv.py's picture scored on each: PSNR against the picture sent, rows
within a pixel of where they belong, as shack_sdr's own score measures
both). The fixtures are ours: our test card through our transmitter,
nothing recorded or downloaded.

sstv.py is loaded from its file, read only; nothing in openhamshack-nb is
imported as a package or changed. It is fed what Radio's engine feeds it:
12 kHz float32 audio in 0.1 s blocks. It decodes four modes (Robot 36,
Martin M1, Scottie S1, PD120); the other ten have no Python figure and are
held to absolute numbers in tests/sstv.rs instead.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from shack_sdr import _native

HERE = Path(__file__).resolve().parent.parent / "testdata" / "sstv"
RATE = 12_000.0
BLOCK = 1200

#: Pictures in each fading condition.
FADES = 8

#: Our ids for the modes sstv.py has, and its names for them.
PY_NAMES = {"robot36": "robot36", "martin1": "martinm1", "scottie1": "scottie_s1", "pd120": "pd120"}


def sweep() -> list[dict]:
    """Every case, in a fixed order with fixed seeds: clean; AWGN down to
    3 dB in 2.5 kHz; tuned 40 Hz high and 100 Hz low; the sender's clock
    200 ppm fast and slow; joined 40 % of the way in (header lost); sent
    with no header at all; and Watterson fading "good" and "moderate",
    FADES pictures each through different fades, judged on their mean (one
    fade can favour either decoder: through ten, Robot 36's rows in place
    ranged 55-95 % for the same decoder)."""
    cases = []

    def add(group, mode, **kw):
        case = {"id": f"{mode}-{group}-{len(cases):03d}", "group": group, "mode": mode, "snr_db": None,
                "offset_hz": 0.0, "clock_ppm": 0.0, "fading": None, "lead_s": 0.4321, "tail_s": 3.0, "cut": 0.0,
                "seed": len(cases) + 1, "vis": True}
        case.update(kw)
        cases.append(case)

    for mode in PY_NAMES:
        add("clean", mode)
        for snr in (30.0, 20.0, 15.0, 10.0, 6.0, 3.0):
            add(f"awgn{snr:g}", mode, snr_db=snr)
        add("offset+40", mode, snr_db=20.0, offset_hz=40.0)
        add("offset-100", mode, snr_db=20.0, offset_hz=-100.0)
        add("clock+200", mode, snr_db=20.0, clock_ppm=200.0)
        add("clock-200", mode, snr_db=20.0, clock_ppm=-200.0)
        add("joined", mode, snr_db=20.0, cut=0.4)
        add("novis", mode, snr_db=20.0, vis=False)
        for fading in ("good", "moderate"):
            for _ in range(FADES):
                add(f"fading-{fading}", mode, snr_db=25.0, fading=fading)
    return cases


def partial(case: dict) -> bool:
    return case["cut"] > 0 or not case["vis"]


def signal(case: dict) -> dict:
    return _native.testsig_sstv(case["mode"], rate=RATE, snr_db=case["snr_db"], offset_hz=case["offset_hz"],
                                clock_ppm=case["clock_ppm"], fading=case["fading"], lead_s=case["lead_s"],
                                tail_s=case["tail_s"], cut=case["cut"], seed=case["seed"], vis=case["vis"])


def load_sstv(nb: Path):
    path = nb / "ohscore" / "modes" / "sstv.py"
    spec = importlib.util.spec_from_file_location("nb_sstv_reference", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def python_case(args) -> dict:
    nb, case = args
    sstv, _ = load_sstv(Path(nb))
    sig = signal(case)
    x = np.frombuffer(sig["samples"], np.float32)
    w, h = sig["width"], sig["height"]
    events = []
    t = time.process_time()
    with tempfile.TemporaryDirectory(dir=os.environ.get("SSTV_GOLDEN_TMP")) as d:
        dec = sstv.Decoder(events.append, out_dir=d)
        for i in range(0, len(x), BLOCK):
            dec.feed(x[i:i + BLOCK], i / RATE)
        cpu = time.process_time() - t
        best = None
        for e in events:
            if e["state"] != "done" or not e.get("path"):
                continue
            pw, ph, rgb = _native.sstv_png_rgb(Path(e["path"]).read_bytes())
            if pw != w:
                continue
            s = _native.sstv_score(sig["image"], w, h, rgb, partial(case))
            if best is None or s["psnr"] > best["psnr"]:
                best = s
    starts = [e for e in events if e["state"] == "start"]
    return {"id": case["id"], "sha256": hashlib.sha256(x.tobytes()).hexdigest(),
            "starts": len(starts), "modes": [e["fmt"] for e in starts],
            "psnr": round(best["psnr"], 3) if best else 0.0,
            "inSync": round(best["inSync"], 4) if best else 0.0,
            "maxShift": round(best["maxShift"], 3) if best else None,
            "rows": best["rows"] if best else 0,
            "cpuS": round(cpu, 2), "audioS": round(len(x) / RATE, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nb", type=Path, default=Path.home() / "openhamshack-nb",
                    help="an openhamshack-nb checkout (read only)")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    a = ap.parse_args()
    _, path = load_sstv(a.nb)
    rev = subprocess.run(["git", "-C", str(a.nb), "log", "-1", "--format=%h", "--", "ohscore/modes/sstv.py"],
                         capture_output=True, text=True).stdout.strip()
    cases = sweep()
    t = time.time()
    with ProcessPoolExecutor(a.jobs) as pool:
        results = list(pool.map(python_case, [(str(a.nb), c) for c in cases]))
    for c, r in zip(cases, results):
        c["sha256"] = r.pop("sha256")
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "cases.json").write_text(json.dumps(
        {"about": "SSTV fixtures: recipes for shack_sdr::sstv::tx (the test card in `mode`, 12 kHz audio, "
                  "SNR in 2.5 kHz) and the sha256 of the float32 samples each makes. "
                  "Written by tools/sstv_golden.py.",
         "rate": RATE, "block": BLOCK, "fades": FADES, "cases": cases}, indent=1) + "\n")
    (HERE / "golden.json").write_text(json.dumps(
        {"about": "How openhamshack-nb ohscore/modes/sstv.py's Decoder did on each fixture in cases.json, "
                  "captured before its removal (RADIO-SDR-PLAN §7.1): its best picture's PSNR against the "
                  "picture sent and the share of its rows within a pixel of their place (shack_sdr's "
                  "sstv_score), the pictures it started and in which modes, and its CPU seconds. "
                  "The Rust decoder is held to never worse.",
         "reference": {"file": "openhamshack-nb ohscore/modes/sstv.py", "commit": rev,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
         "results": results}, indent=1) + "\n")
    print(f"{len(cases)} cases in {time.time() - t:.0f} s")
    for r in results:
        print(f"{r['id']:28} psnr {r['psnr']:6.2f} sync {r['inSync']:.3f} starts {r['starts']} {r['modes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
