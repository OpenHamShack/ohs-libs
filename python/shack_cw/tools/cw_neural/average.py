#!/usr/bin/env python3
"""Average a run's last checkpoints and export the result.

    python3 tools/cw_neural/average.py RUN_DIR OUT.npz [--last 5] [--val VAL_DIR] [--with CK.pt ...]

Checkpoints of one run, a thousand steps apart, read the real bands
differently (the decoder bench's beacon CER moved by 8 points between two
of them); their mean weights usually sit in a flatter place than any one.
Batch norm's running statistics are averaged too, then folded on export as
for any checkpoint. With --val, the averaged network is scored on that
synthetic validation set first. --with adds checkpoints of the same
lineage (a fine-tune averaged with the run it began from).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as M  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run")
    ap.add_argument("out")
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--val")
    ap.add_argument("--with", nargs="*", default=[], dest="extra", metavar="CK",
                    help="other checkpoints (.pt) of the same lineage to average in, e.g. the run this one began from")
    a = ap.parse_args(argv)
    cks = sorted(Path(a.run).expanduser().glob("step-*.pt"))[-a.last:] + [Path(x).expanduser() for x in a.extra]
    if not cks:
        raise SystemExit("no step-*.pt checkpoints in the run")
    states = [torch.load(p) for p in cks]
    cfg = states[0]["config"]
    net = M.build(cfg)
    avg = {}
    for k, v in states[0]["model"].items():
        if v.is_floating_point():
            avg[k] = sum(s["model"][k] for s in states) / len(states)
        else:
            avg[k] = states[-1]["model"][k]           # batch norm's step counter
    net.load_state_dict(avg)
    steps = [s["step"] for s in states]
    info = {"averaged_steps": steps, "averaged": [str(p) for p in cks], "trained": "synthetic"}
    if a.val:
        import train
        ev = train.evaluate(net, train.Shards([a.val], net.front))
        info["val_cer"] = ev["cer"]
        print(f"val CER {ev['cer']:.4f} {ev['by_snr']}")
    M.export(net, a.out, info)
    print(f"averaged {steps} -> {a.out}, export diff {M.check_export(net, a.out):.2e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
