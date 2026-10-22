"""Capture the golden numbers the Rust RTTY decoder is held to (RADIO-SDR-PLAN
§7.1-7.2): openhamshack-nb's Python `ohscore/modes/rtty.py` Decoder run on
every synthetic fixture, before that decoder is removed.

    PYTHONPATH=<dev build> python rust/shack_sdr/tools/rtty_golden.py --nb ~/openhamshack-nb

Writes rust/shack_sdr/testdata/rtty/cases.json (the fixtures: each case's
recipe and the sha256 of the samples testsig makes from it, so the
fixtures are pinned byte for byte) and golden.json (what rtty.py printed
on each, and its character error rate). The fixtures are ours: synthetic
signals from shack_sdr::testsig, nothing recorded or downloaded.

rtty.py is loaded from its file, read only; nothing in openhamshack-nb is
imported as a package or changed. It is fed exactly what Radio's engine
feeds it: 12 kHz float32 audio in 0.1 s blocks, its centre at 1000 Hz, the
case's baud and shift (a reversed signal is a negative shift to it: it has
no other way to say so). It has no AFC, so the off-tune cases show what an
operator who tuned 15-60 Hz off got.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from shack_sdr import _native

HERE = Path(__file__).resolve().parent.parent / "testdata" / "rtty"
RATE = 12_000.0
CENTRE = 1000.0
BLOCK = 1200

#: Our own messages, each a typical over: a CQ, a contest exchange, a
#: ragchew line, figures-heavy weather and RY test lines, and every letter.
TEXTS = [
    "CQ CQ CQ DE GM4BRI GM4BRI GM4BRI PSE K\n",
    "GM4BRI DE DL1ABC UR RST 599 599 NR 0123 0123 BK\n",
    "THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 1234567890\n",
    "WX HERE IS SUNNY 15 DEG C WIND SW 12 KT QNH 1013\n",
    "RYRYRYRYRYRYRYRYRY DE G4XYZ G4XYZ TEST\n",
    "TNX FER CALL DR OM NAME IS BRIAN QTH NR EDINBURGH\n",
]

SNRS = [-10.0, -8.0, -6.0, -5.0, -4.0, -2.0, 0.0, 3.0, 6.0]
SEEDS = 3


def sweep() -> list[dict]:
    """Every case, in a fixed order with fixed seeds."""
    cases = []

    def add(group, **kw):
        seed = len(cases) + 1
        case = {"id": f"{group}-{len(cases):04d}", "group": group, "text": TEXTS[seed % len(TEXTS)],
                "baud": 45.45, "shift": 170.0, "reverse": False, "snr_db": None, "fading": None, "offset_hz": 0.0,
                "drift": 0.0, "lead_s": 1.0, "tail_s": 1.0, "seed": seed}
        case.update(kw)
        cases.append(case)

    for baud in (45.45, 50.0, 75.0):
        for shift in (170.0, 425.0, 450.0, 850.0):
            for snr in SNRS:
                for _ in range(SEEDS):
                    add("awgn", baud=baud, shift=shift, snr_db=snr)
    for offset in (15.0, -30.0, 60.0):
        for snr in (-5.0, 0.0, 6.0):
            for _ in range(SEEDS):
                add("offset", offset_hz=offset, snr_db=snr)
    for snr in (-6.0, -4.0, 0.0, 6.0):
        for _ in range(SEEDS):
            add("reverse", reverse=True, snr_db=snr)
    for fading in ("moderate", "poor"):
        for snr in (0.0, 5.0, 10.0, 20.0):
            for _ in range(2 * SEEDS):
                add("fading", fading=fading, snr_db=snr)
    for snr in (0.0, 6.0):
        for _ in range(SEEDS):
            add("drift", drift=1.0, snr_db=snr)
    # Ten minutes of noise alone, in one-minute cases: what prints is false.
    for _ in range(10):
        add("noise", text="", snr_db=0.0, lead_s=60.0, tail_s=0.0)
    return cases


def audio(case: dict) -> np.ndarray:
    raw = _native.testsig_rtty(case["text"], baud=case["baud"], shift=case["shift"], reverse=case["reverse"],
                               rate=RATE, centre=CENTRE + case["offset_hz"], drift=case["drift"],
                               snr_db=case["snr_db"], fading=case["fading"], lead_s=case["lead_s"],
                               tail_s=case["tail_s"], seed=case["seed"])
    return np.frombuffer(raw, np.float32)


def load_rtty(nb: Path):
    path = nb / "ohscore" / "modes" / "rtty.py"
    spec = importlib.util.spec_from_file_location("nb_rtty_reference", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def python_decode(rtty, case: dict, x: np.ndarray) -> str:
    shift = -case["shift"] if case["reverse"] else case["shift"]
    dec = rtty.Decoder(lambda ev: None, baud=case["baud"], shift=shift, center=CENTRE)
    for i in range(0, len(x), BLOCK):
        dec.feed(x[i:i + BLOCK], i / RATE)
    return dec.text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nb", type=Path, default=Path.home() / "openhamshack-nb",
                    help="an openhamshack-nb checkout (read only)")
    a = ap.parse_args()
    rtty, path = load_rtty(a.nb)
    rev = subprocess.run(["git", "-C", str(a.nb), "log", "-1", "--format=%h", "--", "ohscore/modes/rtty.py"],
                         capture_output=True, text=True).stdout.strip()
    cases = sweep()
    golden = []
    t = time.time()
    for case in cases:
        x = audio(case)
        case["sha256"] = hashlib.sha256(x.tobytes()).hexdigest()
        truth = _native.rtty_truth(case["text"])
        got = python_decode(rtty, case, x)
        golden.append({"id": case["id"], "text": got, "edits": _native.levenshtein(got, truth),
                       "chars": len(truth)})
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "cases.json").write_text(json.dumps(
        {"about": "RTTY fixtures: recipes for shack_sdr::testsig (12 kHz audio, centre 1000 Hz + offset_hz, "
                  "SNR in 2.5 kHz) and the sha256 of the float32 samples each makes. "
                  "Written by tools/rtty_golden.py.",
         "rate": RATE, "centre": CENTRE, "block": BLOCK, "cases": cases}, indent=1) + "\n")
    (HERE / "golden.json").write_text(json.dumps(
        {"about": "What openhamshack-nb ohscore/modes/rtty.py's Decoder printed on each fixture in cases.json, "
                  "captured before its removal (RADIO-SDR-PLAN §7.1). The Rust decoder is held to never worse.",
         "reference": {"file": "openhamshack-nb ohscore/modes/rtty.py", "commit": rev,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
         "results": golden}, indent=1) + "\n")
    print(f"{len(cases)} cases in {time.time() - t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
