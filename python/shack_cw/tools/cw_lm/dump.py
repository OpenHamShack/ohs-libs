#!/usr/bin/env python3
"""The network's outputs on real recordings, kept, so the language model's
search can be tried at many settings without running the network again.

    python3 tools/cw_lm/dump.py dev DEV_DIR OUT [--jobs 12] [--weights W.npz]
    python3 tools/cw_lm/dump.py survey ANALYSIS.json SURVEY_TOOLS OUT [--jobs 12] [--weights W.npz]

The search sits after the network and the squelch and changes neither,
so one pass of Decoder(engine="nn") per recording gives everything any
search setting needs: per network output its log-probabilities, speed and
whether the squelch was open (OUT/<id>.npz, float16). evaluate.py replays
them through Bank._read, the code the live decoder runs.

`dev`: the realval dev set (office /srv/iq/derived/cw-neural/README.txt):
DEV_DIR holds dev-clips.json and dev-calls-1, dev-calls-2, dev-beacons,
each with audio/<id>.npy (the bench receiver's 8 kHz audio).

`survey`: the CW survey's lines (office /srv/iq/derived/cw-survey,
ohs-libs research/cw-survey) on recordings that are neither the test
set's nor the dev set's, in the CW segments, with an RBN-spotted call or
a stretch the engines agree on. Each is cut again from the IQ library as
survey.py cut it (SURVEY_TOOLS: that branch's python/shack_cw/tools, for
survey.open_iq and survey.channels): 8 kHz on the line, the bench
receiver, 700 Hz pitch. The line's windows and RBN calls go in OUT/index.json.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import concurrent.futures as cf  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "decoder_bench"))
sys.path.insert(0, str(HERE))

FS = 8000
PITCH = 700.0
EVAL_GROUPS = ("flex-pair", "flex-survey", "box-probe", "box-wide", "box-pair", "kiwi-cwpseudo", "kiwi-cwtrain")


def outputs(audio: np.ndarray, weights: str | None = None) -> dict:
    """One Decoder(engine="nn") pass over 8 kHz audio in an app's 75 ms
    blocks: the network's outputs as the bank saw them."""
    from shack_cw.nn import NnDecoder

    class Recorder(NnDecoder):
        def reset(self):
            super().reset()
            self.rec = []

        def _from_sums(self, c):
            self._pending.append(c)
            if sum(len(x) for x in self._pending) < self.GATHER:
                return
            c = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
            self._pending = []
            b = self._bank
            b.snr_db = self.snr_db
            b._sync([0])
            logp, wpm, opened = b._outputs(c[:, None, :], self.frame_len)
            if logp.shape[1]:
                self.rec.append((logp[0], wpm[0], opened[:, 0]))

    d = Recorder(None, freq=PITCH, polarity=False, weights=weights)
    blk = int(round(0.075 * FS))
    for i in range(0, len(audio), blk):
        d.feed(audio[i:i + blk], FS)
    if not d.rec:
        return {"logp": np.zeros((0, 46), np.float16), "wpm": np.zeros(0, np.float16), "opened": np.zeros(0, bool)}
    return {"logp": np.concatenate([r[0] for r in d.rec]).astype(np.float16),
            "wpm": np.concatenate([r[1] for r in d.rec]).astype(np.float16),
            "opened": np.concatenate([r[2] for r in d.rec]),
            "out_s": np.float32(d._bank.out_s)}


def _dev_one(job):
    dst, src, weights = job
    if Path(dst).is_file():
        return dst
    o = outputs(np.load(src).astype(np.float32), weights)
    np.savez(dst, **o)
    return dst


def dev(dev_dir: Path, out: Path, jobs: int, weights: str | None) -> None:
    clips = json.loads((dev_dir / "dev-clips.json").read_text())
    audio = {p.stem: p for sub in ("dev-calls-1", "dev-calls-2", "dev-beacons")
             for p in (dev_dir / sub / "audio").glob("*.npy")}
    out.mkdir(parents=True, exist_ok=True)
    (out / "clips.json").write_text(json.dumps(clips))
    todo = [(str(out / f"{c['id']}.npz"), str(audio[c["id"]]), weights) for c in clips]
    with cf.ProcessPoolExecutor(jobs) as ex:
        for d in ex.map(_dev_one, todo):
            print("done", Path(d).name, flush=True)


def _survey_one(job):
    import clips
    dst, c, weights = job
    audio = clips.receive(c)
    o = outputs(audio, weights)
    np.savez(dst, **o)
    return dst


def eval_lines(analysis: dict) -> list[dict]:
    from build import TEST_RECS
    wins: dict = {}
    for w in analysis["windows"]:
        wins.setdefault((w["rec"], round(w["hz"], 1)), []).append(w)
    out = []
    for ln in analysis["lines"]:
        if ln["group"] not in EVAL_GROUPS or ln["rec"] in TEST_RECS or not ln["seg"]:
            continue
        ws = wins.get((ln["rec"], round(ln["hz"], 1)), [])
        if ln["rbn_calls"] or any(w["cls"] == "agree" for w in ws):
            keep = ("k", "a", "b", "cls", "snr", "classic", "nn", "rbn_hit", "causes")
            out.append(dict(ln, windows=[{k: w.get(k) for k in keep} for w in ws]))
    return out


def survey(analysis_path: Path, tools: Path, out: Path, jobs: int, weights: str | None) -> None:
    sys.path.insert(0, str(tools / "cw_survey"))
    import survey as sv
    lines = eval_lines(json.loads(analysis_path.read_text()))
    out.mkdir(parents=True, exist_ok=True)
    for i, ln in enumerate(lines):
        ln["id"] = f"{ln['rec']}-{int(round(ln['hz']))}"
    (out / "index.json").write_text(json.dumps(lines))
    by: dict = {}
    for ln in lines:
        by.setdefault(ln["rec"], []).append(ln)
    print(f"{len(lines)} lines on {len(by)} recordings, {sum(l['seconds'] for l in lines) / 3600:.1f} h", flush=True)
    with cf.ProcessPoolExecutor(jobs) as ex:
        futs = []
        for rid, ls in by.items():
            ls = [x for x in ls if not (out / f"{x['id']}.npz").is_file()]
            if not ls:
                continue
            reader, rate, centre, t0, n = sv.open_iq(rid)
            cs = sv.channels(reader, rate, n, [x["hz"] - centre for x in ls])
            futs += [ex.submit(_survey_one, (str(out / f"{x['id']}.npz"), c, weights)) for x, c in zip(ls, cs)]
            del cs
            print(f"{rid}: {len(ls)} lines cut", flush=True)
            while sum(not f.done() for f in futs) > 3 * jobs:
                cf.wait(futs, return_when=cf.FIRST_COMPLETED)
        for f in cf.as_completed(futs):
            f.result()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("dev")
    a1.add_argument("dev_dir")
    a1.add_argument("out")
    a2 = sub.add_parser("survey")
    a2.add_argument("analysis")
    a2.add_argument("tools")
    a2.add_argument("out")
    for p in (a1, a2):
        p.add_argument("--jobs", type=int, default=12)
        p.add_argument("--weights")
    a = ap.parse_args(argv)
    os.nice(10)
    if a.cmd == "dev":
        dev(Path(a.dev_dir), Path(a.out), a.jobs, a.weights)
    else:
        survey(Path(a.analysis), Path(a.tools), Path(a.out), a.jobs, a.weights)
    return 0


if __name__ == "__main__":
    sys.exit(main())
