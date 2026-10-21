#!/usr/bin/env python3
"""Run the neural CW decoders over the bench's clips.

    ~/build/venvs/cw-nn/bin/python tools/decoder_bench/run_neural.py CLIPS_DIR DECODER
            --src ~/build/cw-decoder-bench/src [--jobs N]

Runs in its own virtualenv (torch, onnxruntime), not ohs-libs': none of
this is a dependency of the library, and the sources and weights are
fetched into --src, outside git:

* `deepfist`: n9bc/DeepFist (GPL-3.0-or-later), release exp27_bt-champion
  (model.pt + config.json in DeepFist/runs/exp27_bt). CNN + temporal
  convolution, CTC, 3200 Hz spectrogram input. Run as its live decoder
  runs it (scripts/tci_decode.py): 6 s windows, its keying squelch
  (tools/squelch.py, threshold 12) on the raw window, then its
  conditioner and the network; here re-decoded every 2 s, keeping the
  characters in each window's central 2 s, so every second is decoded
  three times, as a streaming decoder must.
* `deepcw`: e04/deepcw-engine (AGPL-3.0-only), model.onnx. CTC on the
  same kind of spectrogram; takes 5-20 s. 10 s windows, central 5 s
  kept. No squelch: the engine has none.
* `morseformer`: sderhy/morseformer (Apache-2.0), rnnt_phase11b from the
  Hugging Face hub (HF_HOME=--src/../hf). Conformer encoder, RNN-T head,
  4.1 M parameters. Its `live` preset: 6 s windows every 2 s
  (morseformer.decoding.streaming), confidence 0.6, digits 0.9, a 200 Hz
  front end on our 700 Hz pitch.

Each runs single-threaded (torch.set_num_threads(1), onnxruntime one
intra-op thread), so `cpu` is what one channel costs one core. Characters
are timed at the frame the network emitted them on.

Writes CLIPS_DIR/results/DECODER/<clip>.json as run_classic.py does.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

FS = 8000
PITCH = 700.0

_M = {}


def _windows(n: int, rate: int, win_s: float, hop_s: float):
    """(start, end, commit_lo, commit_hi) in samples: windows `win_s` long
    every `hop_s`, each committing its central `hop_s`; the first commits
    from 0, the last to the end (morseformer's central-zone commit)."""
    win, hop = int(win_s * rate), int(hop_s * rate)
    starts = list(range(0, max(1, n - win + 1), hop))
    if starts[-1] + win < n:
        starts.append(n - win if n > win else 0)
    out = []
    prev = 0
    for k, s in enumerate(starts):
        c = s + win // 2
        lo, hi = (0 if k == 0 else max(prev, c - hop // 2)), c + (hop - hop // 2)
        if k == len(starts) - 1:
            hi = n
        out.append((s, min(n, s + win), lo, max(lo, hi)))
        prev = max(lo, hi)
    return out


def _ctc(lp: np.ndarray, chars, blank: int, t0: float, frame_s: float) -> list[tuple[float, str]]:
    """Greedy CTC over [T, C] log-probs, each emission timed."""
    best = lp.argmax(axis=-1)
    out, prev = [], None
    for i, s in enumerate(best.tolist()):
        if s != prev and s != blank:
            out.append((t0 + (i + 0.5) * frame_s, chars[s]))
        prev = s
    return out


def deepfist(a: np.ndarray, src: Path) -> list[tuple[float, str]]:
    import torch
    if "df" not in _M:
        sys.path[:0] = [str(src / "DeepFist"), str(src / "DeepFist" / "tools")]
        from deepfist.model.net import CwCtcNet
        from deepfist.morse.alphabet import TOKENS
        ck = src / "DeepFist" / "runs" / "exp27_bt"
        cfg = json.loads((ck / "config.json").read_text())
        net = CwCtcNet(time_downsample=cfg["time_downsample"], width=cfg["width"])
        net.load_state_dict(torch.load(ck / "model.pt", map_location="cpu"))
        net.eval()
        _M["df"] = (net, TOKENS)
    net, tokens = _M["df"]
    from deepfist.features.conditioner import condition
    from deepfist.features.spectrogram import audio_to_spectrogram, SAMPLE_RATE
    from squelch import has_signal, DEFAULT_THRESH
    x = resample_poly(a, 2, 5).astype(np.float32)       # 8000 -> 3200
    out = []
    with torch.no_grad():
        for s, e, lo, hi in _windows(len(x), SAMPLE_RATE, 6.0, 2.0):
            seg = x[s:e]
            if len(seg) < SAMPLE_RATE:
                continue
            if not has_signal(seg, SAMPLE_RATE, DEFAULT_THRESH)[0]:
                continue
            spec = audio_to_spectrogram(condition(seg, SAMPLE_RATE), SAMPLE_RATE)
            lp = net(spec.unsqueeze(0).unsqueeze(0))[:, 0, :].numpy()     # [T', C]
            frame = len(seg) / SAMPLE_RATE / lp.shape[0]
            for t, ch in _ctc(lp, tokens, 0, s / SAMPLE_RATE, frame):
                if lo / SAMPLE_RATE <= t < hi / SAMPLE_RATE:
                    out.append((t, ch))
    return out


def deepcw(a: np.ndarray, src: Path) -> list[tuple[float, str]]:
    import onnxruntime as ort
    eng = src / "deepcw-engine"
    if "dc" not in _M:
        sys.path.insert(0, str(eng / "examples" / "python"))
        import decode_morse
        meta = json.loads((eng / "model.onnx.json").read_text())
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(str(eng / "model.onnx"), so, providers=["CPUExecutionProvider"])
        _M["dc"] = (decode_morse, meta, sess)
    mod, meta, sess = _M["dc"]
    sr = int(meta["sample_rate"])
    x = resample_poly(a, 2, 5).astype(np.float32)
    out = []
    for s, e, lo, hi in _windows(len(x), sr, 10.0, 5.0):
        seg = x[s:e]
        if len(seg) < 5 * sr:
            seg = np.pad(seg, (0, 5 * sr - len(seg)))
        lp = sess.run([meta["onnx_output_name"]], {meta["onnx_input_name"]: mod.audio_to_spectrogram(seg, meta)})[0][0]
        frame = len(seg) / sr / lp.shape[0]
        for t, ch in _ctc(lp, meta["chars"], int(meta["blank_index"]), s / sr, frame):
            if lo / sr <= t < hi / sr:
                out.append((t, ch))
    return out


def morseformer(a: np.ndarray, src: Path) -> list[tuple[float, str]]:
    import torch
    if "mf" not in _M:
        os.environ.setdefault("HF_HOME", str(src.parent / "hf"))
        from morseformer.cli.registry import resolve_model
        from morseformer.core.tokenizer import INDEX_TO_TOKEN
        from morseformer.features import FrontendConfig
        from morseformer.models.rnnt import RnntModel
        sys.path.insert(0, str(src / "morseformer"))
        from scripts.decode_audio import _rnnt_cfg_from_state
        ck = torch.load(str(resolve_model("rnnt_phase11b")), map_location="cpu", weights_only=False)
        state = dict(ck["model"])
        model = RnntModel(_rnnt_cfg_from_state(state)).eval()
        model.load_state_dict(state)
        _M["mf"] = (model, FrontendConfig(tone_freq=PITCH, bandwidth=200.0, frame_rate=500), INDEX_TO_TOKEN)
    model, fcfg, vocab = _M["mf"]
    from morseformer.features import extract_features
    out = []
    spf = FS * 4 // 500          # samples per encoder frame (4x subsampling of 500 Hz features)
    with torch.no_grad():
        for s, e, lo, hi in _windows(len(a), FS, 6.0, 2.0):
            feats = extract_features(a[s:e].astype(np.float32), FS, fcfg)
            if feats.shape[0] == 0:
                continue
            x = torch.from_numpy(feats).unsqueeze(0)
            al = model.greedy_rnnt_decode_aligned(x, torch.tensor([feats.shape[0]]), max_emit_per_frame=5,
                                                  confidence_threshold=0.6, digit_threshold=0.9)[0]
            for tok, fr in al:
                smp = s + fr * spf
                if lo <= smp < hi:
                    out.append((smp / FS, vocab[tok]))
    return out


def one(clips_dir: Path, clip: dict, decoder: str, src: Path) -> str:
    import torch
    torch.set_num_threads(1)
    dst = clips_dir / "results" / decoder / f"{clip['id']}.json"
    if dst.is_file():
        return "skip " + clip["id"]
    a = np.load(clips_dir / "audio" / f"{clip['id']}.npy")
    c0 = time.process_time()
    ev = {"deepfist": deepfist, "deepcw": deepcw, "morseformer": morseformer}[decoder](a, src)
    cpu = time.process_time() - c0
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"events": ev, "cpu": cpu, "seconds": len(a) / FS}), encoding="utf-8")
    return f"{clip['id']} {len(ev)} chars {cpu:.1f} s"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clips")
    ap.add_argument("decoder", choices=["deepfist", "deepcw", "morseformer"])
    ap.add_argument("--src", default=str(Path.home() / "build" / "cw-decoder-bench" / "src"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    d = Path(os.path.expanduser(a.clips))
    clips = json.loads((d / "clips.json").read_text(encoding="utf-8"))
    if a.only:
        clips = [c for c in clips if a.only in c["id"]]
    src = Path(os.path.expanduser(a.src))
    with cf.ProcessPoolExecutor(a.jobs) as ex:
        for msg in ex.map(one, [d] * len(clips), clips, [a.decoder] * len(clips), [src] * len(clips)):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
