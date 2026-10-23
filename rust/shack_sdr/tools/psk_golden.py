"""Capture the golden numbers the Rust PSK decoder is held to (RADIO-SDR-PLAN
§7.1, §8.5 M2): openhamshack-nb's Python `ohscore/modes/psk.py` Decoder run
on every synthetic fixture, before that decoder is removed.

    PYTHONPATH=<dev build> python rust/shack_sdr/tools/psk_golden.py --nb ~/openhamshack-nb

Writes rust/shack_sdr/testdata/psk/cases.json (the fixtures: each case's
recipe for shack_sdr's PSK test transmitter, src/psk/tx.rs, and the sha256
of the samples it makes, so the fixtures are pinned byte for byte) and
golden.json (what psk.py printed on each, and its edits against the text
sent). The fixtures are ours: synthetic signals, nothing recorded or
downloaded.

psk.py is loaded from its file, read only; nothing in openhamshack-nb is
imported as a package or changed. It is fed what Radio's engine feeds it:
12 kHz float32 audio in 0.1 s blocks, its centre at 1000 Hz, the variant
(psk31, psk63, psk125) for the case's baud, and its own default AFC range
(+-15 Hz), which every off-tune and drifting case stays inside.

SNR is in 2.5 kHz, as for RTTY (testsig::SNR_BANDWIDTH). In the symbol's
own bandwidth, where psk.py's docstring quotes it, add 19 dB at 31.25 Bd,
16 dB at 62.5 and 13 dB at 125.
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

HERE = Path(__file__).resolve().parent.parent / "testdata" / "psk"
RATE = 12_000.0
CENTRE = 1000.0
BLOCK = 1200
VARIANTS = {31.25: "psk31", 62.5: "psk63", 125.0: "psk125"}

#: Our own messages, each a typical PSK over: mixed case, as PSK31 is
#: typed, with the figures and punctuation of a real exchange.
TEXTS = [
    "CQ CQ CQ de GM4BRI GM4BRI GM4BRI pse k\n",
    "GM4BRI de DL1ABC ur rst 599 599 name Hans qth Berlin btu\n",
    "The quick brown fox jumps over the lazy dog 1234567890\n",
    "Rig here is an IC-7300 at 30 W into a dipole at 10 m.\n",
    "tnx fer the nice qso Brian, 73 and gl de DL1ABC sk\n",
    "WX here: sunny, 15 C, wind SW 12 kt; QNH 1013 hPa.\n",
]

#: SNR (dB in 2.5 kHz) grid at 31.25 Bd; each faster speed needs 3 dB more
#: for the same energy per bit.
SNRS = [-16.0, -14.0, -13.0, -12.0, -11.0, -10.0, -8.0, -5.0]
SEEDS = 3


def up(baud: float) -> float:
    return {31.25: 0.0, 62.5: 3.0, 125.0: 6.0}[baud]


def sweep() -> list[dict]:
    """Every case, in a fixed order with fixed seeds."""
    cases = []

    def add(group, **kw):
        seed = len(cases) + 1
        case = {"id": f"{group}-{len(cases):04d}", "group": group, "text": TEXTS[seed % len(TEXTS)],
                "baud": 31.25, "snr_db": None, "fading": None, "offset_hz": 0.0, "drift": 0.0, "clock_ppm": 0.0,
                "interferer": None, "lead_s": 1.0, "tail_s": 2.0, "seed": seed}
        case.update(kw)
        cases.append(case)

    for baud in (31.25, 62.5, 125.0):
        for snr in SNRS:
            for _ in range(SEEDS):
                add("awgn", baud=baud, snr_db=snr + up(baud))
    for baud in (31.25, 62.5):
        for offset in (7.0, -13.0):
            for snr in (-11.0, -8.0):
                for _ in range(SEEDS):
                    add("offset", baud=baud, offset_hz=offset, snr_db=snr + up(baud))
        for snr in (-11.0, -8.0):
            for _ in range(SEEDS):
                add("drift", baud=baud, offset_hz=-8.0, drift=0.5, snr_db=snr + up(baud))
        for _ in range(SEEDS):
            # A sound card 500 ppm fast: the symbol clock slides half a symbol
            # in 30 s.
            add("clock", baud=baud, clock_ppm=500.0, snr_db=-10.0 + up(baud))
        for fading in ("moderate", "poor"):
            for snr in (-8.0, -4.0, 0.0, 5.0):
                for _ in range(4):
                    add("fading", baud=baud, fading=fading, snr_db=snr + up(baud))
        # A second station close by: 1.6 and 2.2 channel widths away, as
        # strong as the wanted one and 10 dB stronger.
        for offset in (1.6 * baud, -2.24 * baud):
            for level in (0.0, 10.0):
                for _ in range(SEEDS):
                    add("interferer", baud=baud, snr_db=-8.0 + up(baud),
                        interferer=[offset, level, baud, TEXTS[(len(cases) + 3) % len(TEXTS)]])
    # Noise alone in one-minute cases: what prints is false.
    for baud, n in ((31.25, 10), (62.5, 5)):
        for _ in range(n):
            add("noise", baud=baud, text="", snr_db=0.0, lead_s=60.0, tail_s=0.0)
    return cases


def audio(case: dict) -> np.ndarray:
    i = case["interferer"]
    raw = _native.testsig_psk(case["text"], baud=case["baud"], rate=RATE, centre=CENTRE + case["offset_hz"],
                              drift=case["drift"], snr_db=case["snr_db"], fading=case["fading"],
                              interferer=tuple(i) if i else None, lead_s=case["lead_s"], tail_s=case["tail_s"],
                              seed=case["seed"], clock_ppm=case["clock_ppm"])
    return np.frombuffer(raw, np.float32)


def load_psk(nb: Path):
    path = nb / "ohscore" / "modes" / "psk.py"
    spec = importlib.util.spec_from_file_location("nb_psk_reference", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def python_decode(psk, case: dict, x: np.ndarray) -> str:
    dec = psk.Decoder(lambda ev: None, variant=VARIANTS[case["baud"]], center=CENTRE)
    for i in range(0, len(x), BLOCK):
        dec.feed(x[i:i + BLOCK], i / RATE)
    return dec.text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nb", type=Path, default=Path.home() / "openhamshack-nb",
                    help="an openhamshack-nb checkout (read only)")
    a = ap.parse_args()
    psk, path = load_psk(a.nb)
    rev = subprocess.run(["git", "-C", str(a.nb), "log", "-1", "--format=%h", "--", "ohscore/modes/psk.py"],
                         capture_output=True, text=True).stdout.strip()
    cases = sweep()
    golden = []
    t = time.time()
    for case in cases:
        x = audio(case)
        case["sha256"] = hashlib.sha256(x.tobytes()).hexdigest()
        truth = _native.psk_truth(case["text"])
        got = python_decode(psk, case, x)
        golden.append({"id": case["id"], "text": got, "edits": _native.levenshtein(got, truth),
                       "chars": len(truth)})
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "cases.json").write_text(json.dumps(
        {"about": "PSK fixtures: recipes for shack_sdr's PSK test transmitter, src/psk/tx.rs (12 kHz audio, "
                  "carrier at 1000 Hz + offset_hz, SNR in 2.5 kHz; interferer = [offset_hz, level_db, baud, "
                  "text]) and the sha256 of the float32 samples each makes. Written by tools/psk_golden.py.",
         "rate": RATE, "centre": CENTRE, "block": BLOCK, "cases": cases}, indent=1) + "\n")
    (HERE / "golden.json").write_text(json.dumps(
        {"about": "What openhamshack-nb ohscore/modes/psk.py's Decoder printed on each fixture in cases.json, "
                  "captured before its removal (RADIO-SDR-PLAN §7.1). The Rust decoder is held to never worse.",
         "reference": {"file": "openhamshack-nb ohscore/modes/psk.py", "commit": rev,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
         "results": golden}, indent=1) + "\n")
    print(f"{len(cases)} cases in {time.time() - t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
