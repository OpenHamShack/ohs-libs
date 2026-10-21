#!/usr/bin/env python3
"""Run the classic (rule-based) decoders over the bench's clips.

    python3 tools/decoder_bench/run_classic.py CLIPS_DIR DECODER [--ggm-run PATH] [--jobs N]

DECODER is one of:

* `shack_cw`: ours, as Radio runs it - shack_cw.Decoder(polarity=False)
  fed the receiver's audio in 75 ms blocks (Radio's CW_POLARITY, and the
  block size its engine hands the decoder). Each character is timed at the
  end of the block it came out of.
* `fldigi`: fldigi 4.2.06's CW receiver at its defaults (fldigi_rx.py, a
  port of cw.cxx's receive path).
* `ggmorse`: ggerganov/ggmorse (MIT), built from source with
  ggm_run.cpp, the pitch given as 700 Hz and the speed left to it.
* `shack_cw_nn`: ours with the neural engine (shack_cw.nn), as Radio
  would run it: Decoder(engine="nn", polarity=False), 75 ms blocks.
  `--weights` scores other weights than the shipped ones and `--name`
  writes them under another results folder, so runs sit side by side.

Every decoder is told the pitch (700 Hz, where clips.py put the signal):
the bench measures decoding, not finding the signal, which is the
skimmer's front end and is the same for any decoder behind it.

Writes CLIPS_DIR/results/DECODER/<clip>.json: {"events": [[t, text], ...],
"cpu": seconds of CPU, "seconds": seconds of audio}.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

FS = 8000
PITCH = 700.0
HERE = Path(__file__).resolve().parent


def run_shack_cw(a: np.ndarray) -> list[tuple[float, str]]:
    from shack_cw.decoder import Decoder
    dec = Decoder(None, freq=PITCH, polarity=False)
    blk = int(round(0.075 * FS))
    out = []
    for i in range(0, len(a), blk):
        t = min(i + blk, len(a)) / FS
        out.extend((t, e["text"]) for e in dec.feed(a[i:i + blk], FS))
    return out


def run_shack_cw_nn(a: np.ndarray, weights: str | None = None, snr_db: float = 6.0,
                    space: float | None = None, space_lm: float | None = None,
                    space_table: str | None = None) -> list[tuple[float, str]]:
    from shack_cw.decoder import Decoder
    dec = Decoder(None, freq=PITCH, polarity=False, engine="nn", weights=weights, snr_db=snr_db, space_bonus=space,
                  space_lm=space_lm, space_table=space_table)
    blk = int(round(0.075 * FS))
    out = []
    for i in range(0, len(a), blk):
        t = min(i + blk, len(a)) / FS
        out.extend((t, e["text"]) for e in dec.feed(a[i:i + blk], FS))
    return out


def run_fldigi(a: np.ndarray) -> list[tuple[float, str]]:
    sys.path.insert(0, str(HERE))
    from fldigi_rx import FldigiCw
    return FldigiCw(PITCH).decode(a)


def run_ggmorse(a: np.ndarray, exe: str) -> tuple[list[tuple[float, str]], float]:
    with tempfile.NamedTemporaryFile(suffix=".f32", dir=os.environ.get("BENCH_TMP")) as f:
        a.astype("<f4").tofile(f.name)
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        p = subprocess.run([exe, f.name, str(FS), str(PITCH)], capture_output=True, text=True, check=True)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    out = []
    for ln in p.stderr.splitlines():
        t, _, txt = ln.partition("\t")
        # ggmorse prints a newline whenever its pitch estimate moves; that
        # is not text.
        out.extend((float(t), ch) for ch in txt.replace("|", ""))
    return out, cpu


def one(clips_dir: Path, clip: dict, decoder: str, exe: str, name: str | None = None, weights: str | None = None,
        snr_db: float = 6.0, space: float | None = None, space_lm: float | None = None,
        space_table: str | None = None) -> str:
    dst = clips_dir / "results" / (name or decoder) / f"{clip['id']}.json"
    if dst.is_file():
        return "skip " + clip["id"]
    a = np.load(clips_dir / "audio" / f"{clip['id']}.npy")
    c0 = time.process_time()
    if decoder == "shack_cw":
        ev = run_shack_cw(a)
        cpu = time.process_time() - c0
    elif decoder == "shack_cw_nn":
        ev = run_shack_cw_nn(a, weights, snr_db, space, space_lm, space_table)
        cpu = time.process_time() - c0
    elif decoder == "fldigi":
        ev = run_fldigi(a)
        cpu = time.process_time() - c0
    elif decoder == "ggmorse":
        ev, cpu = run_ggmorse(a, exe)
    else:
        raise SystemExit(f"unknown decoder {decoder}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"events": ev, "cpu": cpu, "seconds": len(a) / FS}), encoding="utf-8")
    return f"{clip['id']} {len(ev)} chars {cpu:.1f} s"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clips")
    ap.add_argument("decoder", choices=["shack_cw", "shack_cw_nn", "fldigi", "ggmorse"])
    ap.add_argument("--name", help="results folder (default: the decoder's name)")
    ap.add_argument("--weights", help="shack_cw_nn: weights other than the shipped ones")
    ap.add_argument("--snr-db", type=float, default=6.0, help="shack_cw_nn: the squelch's threshold")
    ap.add_argument("--space", type=float,
                    help="shack_cw_nn: log-probability bonus for a word space (default: the engine's)")
    ap.add_argument("--space-lm", type=float, help="shack_cw_nn: the space model's weight (default: the engine's)")
    ap.add_argument("--space-table", help="shack_cw_nn: the space model's table (cw_neural/spacelm.py)")
    ap.add_argument("--ggm-run", default=str(Path.home() / "build" / "cw-decoder-bench" / "harness" / "ggm_run"))
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    d = Path(os.path.expanduser(a.clips))
    clips = json.loads((d / "clips.json").read_text(encoding="utf-8"))
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        n = len(clips)
        for msg in ex.map(one, [d] * n, clips, [a.decoder] * n, [a.ggm_run] * n, [a.name] * n, [a.weights] * n,
                          [a.snr_db] * n, [a.space] * n, [a.space_lm] * n, [a.space_table] * n):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
