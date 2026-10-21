#!/usr/bin/env python3
"""What one decoder channel costs one core, measured alone.

    ~/build/venvs/cw-nn/bin/python tools/decoder_bench/cpu.py CLIPS_DIR [--clip ID] [--repeat 3]

The bench's runs measure CPU too, but several jobs at once on a shared
VM; this times each decoder single-threaded on one clip with nothing else
running, best of --repeat. The skimmer's budget (CW-SKIMMER-PLAN §2,
Brian 13/10) is about 0.4 % of an i5-9400T core per channel: 4 ms of CPU
per second of audio.

Besides the decoders as the bench runs them, three ways of cutting the
neural cost are timed, because a skimmer would not run a model the way a
single-signal decoder does:

* `deepcw-nooverlap`: DeepCW on 10 s windows end to end (no re-decoding;
  costs accuracy at the joins), batch 1.
* `deepcw-batch64`: the same, 64 channels' windows in one call, per
  channel: what a skimmer pays per channel when it batches.
* `tcn-*`: an untrained causal temporal convolution network of the size
  this report proposes (`small_tcn`): per channel, 9 STFT bins round the
  tone (the 23 Hz bins the skimmer already computes) at 100 frames a
  second, 6 dilated layers, CTC over 42 symbols. It is streamed - each
  frame is computed once - so there is no window overlap to pay for.
  Untrained weights cost the same as trained ones; this times the
  arithmetic, not the accuracy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def best(fn, repeat: int) -> float:
    out = []
    for _ in range(repeat):
        c0 = time.process_time()
        fn()
        out.append(time.process_time() - c0)
    return min(out)


def small_tcn(width: int = 64, layers: int = 6, n_in: int = 9, n_out: int = 42):
    import torch
    from torch import nn

    class Tcn(nn.Module):
        def __init__(self):
            super().__init__()
            self.inp = nn.Conv1d(n_in, width, 1)
            self.convs = nn.ModuleList(nn.Conv1d(width, width, 3, dilation=2 ** i) for i in range(layers))
            self.out = nn.Conv1d(width, n_out, 1)

        def forward(self, x):            # [B, n_in, T]
            x = torch.relu(self.inp(x))
            for i, c in enumerate(self.convs):
                pad = 2 * 2 ** i                       # causal: look back only
                x = x + torch.relu(c(nn.functional.pad(x, (pad, 0))))
            return self.out(x).log_softmax(1)
    return Tcn().eval()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clips")
    ap.add_argument("--clip", default="kiwi-20m-noise-wessex-0")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--src", default=str(Path.home() / "build" / "cw-decoder-bench" / "src"))
    a = ap.parse_args(argv)
    import torch
    torch.set_num_threads(1)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    d = Path(os.path.expanduser(a.clips))
    src = Path(os.path.expanduser(a.src))
    x = np.load(d / "audio" / f"{a.clip}.npy")
    secs = len(x) / 8000
    import run_classic as rc
    import run_neural as rn
    rows = {}
    rows["shack_cw (feed)"] = best(lambda: rc.run_shack_cw(x), a.repeat)
    from shack_cw import bench as sb
    rows["shack_cw (skimmer back end, feed_frames)"] = min(sb.backend_us() for _ in range(a.repeat)) * secs / 1e6
    rows["fldigi port (Python)"] = best(lambda: rc.run_fldigi(x), a.repeat)
    exe = str(Path.home() / "build" / "cw-decoder-bench" / "harness" / "ggm_run")
    rows["ggmorse (C++)"] = min(rc.run_ggmorse(x, exe)[1] for _ in range(a.repeat))
    for name in ("deepfist", "deepcw", "morseformer"):
        fn = getattr(rn, name)
        fn(x[:80000], src)                          # load the model outside the timing
        rows[name + " (as run)"] = best(lambda: fn(x, src), a.repeat)
    # DeepFist as run skips windows its squelch shuts; the cost with it open:
    import deepfist.features.conditioner  # noqa: F401  (loaded by the call above)
    import squelch
    keep = squelch.has_signal
    squelch.has_signal = lambda *k, **kw: (True, 99.0)
    rows["deepfist (squelch open)"] = best(lambda: rn.deepfist(x, src), a.repeat)
    squelch.has_signal = keep
    # DeepCW without overlap, batch 1 and batch 64.
    mod, meta, sess = rn._M["dc"]
    from scipy.signal import resample_poly
    y = resample_poly(x, 2, 5).astype(np.float32)
    w = 10 * 3200
    segs = [mod.audio_to_spectrogram(y[i:i + w], meta) for i in range(0, len(y) - w + 1, w)]
    inn, outn = meta["onnx_input_name"], meta["onnx_output_name"]
    rows["deepcw-nooverlap"] = best(lambda: [sess.run([outn], {inn: s}) for s in segs], a.repeat)
    batch = np.concatenate([segs[0]] * 64, axis=0)
    t64 = best(lambda: sess.run([outn], {inn: batch}), a.repeat)
    rows["deepcw-batch64 (per channel)"] = t64 / 64 * (secs / 10.0)
    # The small streaming TCN, 64 channels at once.
    for width in (32, 64):
        net = small_tcn(width)
        frames = int(secs * 100)
        feat = torch.randn(64, 9, frames)
        with torch.no_grad():
            tt = best(lambda: net(feat), a.repeat)
        params = sum(p.numel() for p in net.parameters())
        rows[f"tcn-{width} ({params // 1000}k params, per channel of 64)"] = tt / 64
    out = {k: {"ms_per_s": 1000 * v / secs, "pct_core": 100 * v / secs, "x_budget": (1000 * v / secs) / 4.0}
           for k, v in rows.items()}
    print(f"{'decoder':44s} {'ms/s':>7s} {'% core':>7s} {'x budget':>9s}   ({a.clip}, {secs:.0f} s)")
    for k, v in out.items():
        print(f"{k:44s} {v['ms_per_s']:7.2f} {v['pct_core']:7.2f} {v['x_budget']:9.1f}")
    (d / "cpu.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
