#!/usr/bin/env python3
"""Real validation clips: RBN-spotted calls in fresh KiwiSDR recordings
the decoder bench never saw, cut exactly as the bench cuts its own.

    tools/cw_corpus.py record PLAN.json --dir DIR      # with tools/rbn.py running alongside
    python3 tools/cw_neural/realval.py DIR OUT_DIR

Synthetic validation chooses settings and checkpoints, but synthetic CW
is only as real as its generator. These are real signals weakly labelled
by the RBN (the call, not the text), recorded for this purpose (13/10/2026,
five Kiwis, ten minutes, ohs-dev ~/build/cw-realval), so a choice can be
checked on real bands without touching the bench's 136 clips, which stay
the test set.

Writes OUT_DIR/clips.json and OUT_DIR/audio/*.npy in decoder_bench's
layout, so run_classic.py and score.py work on it unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "decoder_bench"))

import clips  # noqa: E402
from shack_cw.corpus import load_iq  # noqa: E402
from shack_cw.skim import bench as sbench  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", help="a cw_corpus.py recording folder: manifest.jsonl, iq/, rbn-spots.txt")
    ap.add_argument("out")
    a = ap.parse_args(argv)
    base = Path(a.dir).expanduser()
    spots = base / "rbn-spots.txt"
    rbn = sbench.read_rbn(spots) if spots.is_file() else []
    w = clips.Writer(Path(a.out).expanduser())
    for ln in (base / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(ln)
        z, rate = load_iq(base / rec["file"])
        if rec["kind"] == "beacon":
            # NCDXF/IARU beacons: the schedule is the truth (which beacon
            # keys each 10 s slot), as the bench's beacon clips are made.
            from shack_cw.corpus import beacon_slots
            t0 = datetime.fromisoformat(rec["utc"]).timestamp()
            clips.kiwi(w, [dict(rec, targets=[beacon_slots(z, rate, rec["khz"], t0)])], base)
            continue
        centre = float(rec["khz"]) * 1e3
        t0 = datetime.fromisoformat(rec["utc"]).timestamp()
        # The Kiwi's IQ is +-5 kHz; keep a spot's channel inside it.
        clips.rbn_calls(z, rate, centre, t0, rbn, centre - 4500.0, centre + 4500.0, w, rec["band"], "realval",
                        rec["id"])
    w.save()
    print(f"{len(w.clips)} clips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
