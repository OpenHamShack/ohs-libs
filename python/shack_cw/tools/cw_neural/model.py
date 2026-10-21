"""The network in torch, for training only, and its export to the .npz
that shack_cw.nn.Net runs with NumPy.

The shape (shack_cw.nn's docstring says why): a strided convolution from
200 to 50 frames a second, then residual blocks of a causal dilated
convolution (kernel 3), batch norm, ReLU and a 1x1 mix, then a CTC head
and a speed head. Batch norm is folded into the convolution before it on
export, so the runtime has none. Every layer pads on the left only, so
streaming in NumPy gives what training saw; a look-ahead block (round 2)
is still a causal convolution, with its residual taken from its middle
tap, so the network's outputs come later instead of seeing the future.

torch is never a dependency of shack_cw: it is used here, in a training
venv (~/build/venvs/cw-train).
"""
from __future__ import annotations

import json

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from shack_cw.nn import FRONTS, SYMBOLS, n_feat


class CwNet(nn.Module):
    def __init__(self, width: int = 48, dilations=(1, 2, 4, 8, 16, 32, 64), stride: int = 4, k_down: int = 8,
                 front: str = "v1", ahead=()):
        super().__init__()
        self.width, self.dilations, self.stride, self.k_down = width, tuple(dilations), stride, k_down
        self.front = front
        self.n_feat = n_feat(front)
        # Which blocks look ahead (shack_cw.nn.Net): given as dilations;
        # each such block reads t - d, t, t + d.
        self.ahead = tuple(d in set(ahead) for d in self.dilations)
        self.delay = sum(d for d, a in zip(self.dilations, self.ahead) if a)
        self.down = nn.Conv1d(self.n_feat, width, k_down, stride=stride)
        self.convs = nn.ModuleList(nn.Conv1d(width, width, 3, dilation=d) for d in self.dilations)
        self.norms = nn.ModuleList(nn.BatchNorm1d(width) for _ in self.dilations)
        self.mixes = nn.ModuleList(nn.Conv1d(width, width, 1) for _ in self.dilations)
        self.out = nn.Conv1d(width, len(SYMBOLS), 1)
        self.wpm = nn.Conv1d(width, 1, 1)
        for m in self.mixes:              # each block starts near the identity
            nn.init.normal_(m.weight, std=0.02)
            nn.init.zeros_(m.bias)

    def forward(self, x: torch.Tensor):
        """x [B, n_feat, T] -> (log-probs [B, S, T/stride], log(wpm/20) [B, T/stride]).

        Output j is for input frame (j + 1) * stride less `delay` outputs,
        as the NumPy runtime streams it: a look-ahead block is the causal
        convolution with its residual taken d frames back, so training and
        streaming are one arithmetic (check_export) and the network's
        start is padded exactly as a fresh stream's is."""
        x = F.relu(self.down(F.pad(x, (self.k_down - self.stride, 0))))
        for conv, bn, mix, d, ah in zip(self.convs, self.norms, self.mixes, self.dilations, self.ahead):
            h = F.relu(bn(conv(F.pad(x, (2 * d, 0)))))
            res = F.pad(x, (d, 0))[:, :, :x.shape[2]] if ah else x
            x = res + mix(h)
        return self.out(x).log_softmax(1), self.wpm(x)[:, 0]

    def params(self) -> int:
        return sum(p.numel() for p in self.parameters())

    def config(self) -> dict:
        return {"width": self.width, "dilations": list(self.dilations), "stride": self.stride, "k_down": self.k_down,
                "front": self.front, "ahead": [d for d, a in zip(self.dilations, self.ahead) if a]}


def build(cfg: dict) -> CwNet:
    """A CwNet from config() (a checkpoint's): round 1's have no front or ahead."""
    return CwNet(cfg["width"], cfg["dilations"], cfg["stride"], cfg["k_down"], cfg.get("front", "v1"),
                 cfg.get("ahead", ()))


def export(model: CwNet, path: str, info: dict | None = None) -> None:
    """Write the .npz shack_cw.nn.Net loads: weights as matrices laid out
    for `taps @ W` (tap-major), batch norm folded in."""
    model = model.eval()
    g = lambda t: t.detach().cpu().double().numpy()
    w = g(model.down.weight)                                      # (C, F, kd)
    out = {"down_w": w.transpose(2, 1, 0).reshape(-1, w.shape[0]), "down_b": g(model.down.bias)}
    for i, (conv, bn, mix) in enumerate(zip(model.convs, model.norms, model.mixes)):
        s = g(bn.weight) / np.sqrt(g(bn.running_var) + bn.eps)
        cw = g(conv.weight) * s[:, None, None]                    # (C, C, 3)
        cb = (g(conv.bias) - g(bn.running_mean)) * s + g(bn.bias)
        out[f"conv{i}_w"] = cw.transpose(2, 1, 0).reshape(-1, cw.shape[0])
        out[f"conv{i}_b"] = cb
        out[f"mix{i}_w"] = g(mix.weight)[:, :, 0].T
        out[f"mix{i}_b"] = g(mix.bias)
    out["out_w"], out["out_b"] = g(model.out.weight)[:, :, 0].T, g(model.out.bias)
    out["wpm_w"], out["wpm_b"] = g(model.wpm.weight)[:, :, 0].T, g(model.wpm.bias)
    cfg = model.config()
    cfg["ahead"] = list(model.ahead)          # the runtime's form: one flag a block
    meta = dict(cfg, symbols=SYMBOLS, n_feat=model.n_feat, windows=list(FRONTS[model.front]["windows"]),
                **(info or {}))
    np.savez_compressed(path, meta=np.array(json.dumps(meta)), **{k: v.astype(np.float32) for k, v in out.items()})


def check_export(model: CwNet, path: str, frames: int = 1600, seed: int = 0) -> float:
    """Largest difference between torch on a whole sequence and the NumPy
    runtime streamed in uneven blocks over the same features: the export
    and the streaming are both right only if this is tiny."""
    from shack_cw.nn import Net
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((2, frames, model.n_feat)).astype(np.float32)
    with torch.no_grad():
        lp, _ = model.eval()(torch.from_numpy(x.transpose(0, 2, 1)))
    ref = lp.numpy().transpose(0, 2, 1)
    net = Net(path)
    st = net.state(2)
    outs, i = [], 0
    for blk in rng.integers(1, 40, 1000):
        if i >= frames:
            break
        o, _ = net.step(x[:, i:i + blk], st)
        outs.append(o)
        i += blk
    got = np.concatenate(outs, axis=1)
    return float(np.abs(got - ref[:, :got.shape[1]]).max())
