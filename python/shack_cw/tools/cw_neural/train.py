#!/usr/bin/env python3
"""Train the CW network on synthetic shards (data.py), CTC, on the CPU.

    nice -n 10 ~/build/venvs/cw-train/bin/python tools/cw_neural/train.py \\
        --data ~/build/cw-neural/train --val ~/build/cw-neural/val --out ~/build/cw-neural/runs/r1 \\
        --width 48 --steps 40000 --threads 6 [--front v2] [--ahead 1,2,4,8]

--front picks the features (shack_cw.nn.FRONTS) made from the shards'
sums; --ahead the blocks that look ahead (shack_cw.nn.Net), by dilation.

Checkpoints are compared on synthetic validation here and on realval's
real dev set (realval.py) afterwards, never on the decoder bench's clips:
those are the test set (score.py), scored only at a milestone.

Each --eval-every steps: the validation loss, greedy character error
rate overall and per SNR band, and characters printed on examples with
no wanted signal (per minute, before any squelch). The best by
validation CER is exported as best.npz (shack_cw.nn's format) and its
export checked against torch (model.check_export).

New shards appearing in --data while training runs (a generator still
going) are picked up at the next pass over the data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as M  # noqa: E402
from shack_cw.nn import BLANK, SYMBOLS  # noqa: E402


def lev(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def features_of(z, front: str) -> np.ndarray:
    """A shard's features for `front` (N, frames, n_feat) float16, from its
    sums through the runtime's own Features, a hundred examples at a time."""
    from data import unpack
    from shack_cw.nn import Features
    if "sums" not in z.files:
        if front != "v1":
            raise SystemExit(f"a shard of v1 features cannot train front {front}")
        return z["feat"]
    sums, out = z["sums"], []
    for a in range(0, len(sums), 100):
        c = unpack(sums[a:a + 100]).transpose(1, 0, 2)              # (frames, K, columns)
        out.append(Features(c.shape[1], front)(c).transpose(1, 0, 2).astype(np.float16))
    return np.concatenate(out)


def feature_cache(p: Path, front: str, z=None) -> Path:
    """The shard's feature file for `front` beside it, made if missing
    (round 1's feature shards: their own .feat.npy)."""
    z = np.load(p) if z is None else z
    cache = p.with_suffix(".feat.npy" if "feat" in z.files else f".feat-{front}.npy")
    if not cache.is_file():
        # Per process: two runs starting together each write their own and
        # the rename is atomic, so neither reads half a file.
        tmp = p.with_suffix(f".feat.{os.getpid()}.tmp.npy")
        np.save(tmp, features_of(z, front))
        tmp.replace(cache)
    return cache


class Shards:
    """Every shard in some folders, features memory-mapped.

    Each shard's features for the front end being trained are written once
    beside it (.feat-<front>.npy; round 1's .feat.npy for a shard that
    held v1 features already) and mapped, so two runs training at once
    share one copy in the page cache; three sets loaded into each process
    were 22 GB apiece. A shard of sums (data.py) makes any front's
    features through the runtime's own Features, a few hundred examples at
    a time."""

    CHUNK = 100

    def __init__(self, dirs: list[str], front: str = "v1"):
        self.dirs = [Path(d).expanduser() for d in dirs]
        self.front = front
        self.seen: set[str] = set()
        self.feat: list[np.ndarray] = []
        self.where: list[tuple[int, int]] = []          # example -> (shard, row)
        self.labels: list[list[int]] = []
        self.meta = {"wpm": [], "t_first": [], "t_last": [], "snr": [], "kind": []}
        self.refresh()

    def refresh(self) -> int:
        new = 0
        for d in self.dirs:
            for p in sorted(d.glob("shard-*.npz")):
                if p.name in self.seen or ".tmp" in p.name:
                    continue
                z = np.load(p)
                cache = feature_cache(p, self.front, z)
                self.seen.add(p.name)
                k = len(self.feat)
                self.feat.append(np.load(cache, mmap_mode="r"))
                lab, off = z["labels"], z["lab_off"]
                self.labels += [lab[off[i]:off[i + 1]].astype(np.int64).tolist() for i in range(len(off) - 1)]
                self.where += [(k, i) for i in range(len(off) - 1)]
                for key in self.meta:
                    self.meta[key] += z[key].tolist()
                new += len(off) - 1
        return new

    def __len__(self) -> int:
        return len(self.labels)

    def batch(self, idx, tail: int = 0):
        """Features [B, n_feat, frames + tail] (the tail zeros, so a
        look-ahead network's last outputs exist), targets, target lengths."""
        xs = np.stack([self.feat[self.where[i][0]][self.where[i][1]] for i in idx]).astype(np.float32)
        x = torch.from_numpy(xs).transpose(1, 2).contiguous()
        if tail:
            x = F.pad(x, (0, tail))
        labs = [self.labels[i] for i in idx]
        tgt = torch.tensor([s for lab in labs for s in lab], dtype=torch.long)
        tl = torch.tensor([len(lab) for lab in labs], dtype=torch.long)
        return x, tgt, tl


def wpm_target(sh: Shards, idx, frames: int, out_s: float, delay: int = 0):
    """log(wpm/20) and a mask over the outputs inside each example's keying
    (output j describes ((j + 1) - delay) * out_s)."""
    t = (np.arange(frames) + 1 - delay) * out_s
    tg, mk = np.zeros((len(idx), frames), np.float32), np.zeros((len(idx), frames), np.float32)
    for j, i in enumerate(idx):
        w = sh.meta["wpm"][i]
        if w > 0:
            tg[j] = np.log(w / 20.0)
            mk[j] = (t >= sh.meta["t_first"][i]) & (t <= sh.meta["t_last"][i] + 0.5)
    return torch.from_numpy(tg), torch.from_numpy(mk)


def greedy(lp: np.ndarray) -> list[int]:
    best = lp.argmax(0)
    out, prev = [], BLANK
    for s in best.tolist():
        if s != prev and s != BLANK:
            out.append(s)
        prev = s
    while out and out[0] == 1:
        out.pop(0)
    while out and out[-1] == 1:
        out.pop()
    return out


def evaluate(net, val: Shards, batch: int = 64) -> dict:
    net.eval()
    tot_loss, n = 0.0, 0
    errs, chars = {}, {}
    false_chars, false_s = 0, 0.0
    with torch.no_grad():
        for a in range(0, len(val), batch):
            idx = np.arange(a, min(len(val), a + batch))
            x, tgt, tl = val.batch(idx, tail=net.delay * net.stride)
            lp, _ = net(x)
            T = lp.shape[2]
            loss = F.ctc_loss(lp.permute(2, 0, 1), tgt, torch.full((len(idx),), T, dtype=torch.long), tl,
                              blank=BLANK, reduction="sum", zero_infinity=True)
            tot_loss += float(loss)
            n += len(idx)
            lpn = lp.numpy()
            for j, i in enumerate(idx):
                hyp, ref = greedy(lpn[j]), val.labels[i]
                kind, snr = val.meta["kind"][i], val.meta["snr"][i]
                if kind != "signal" or not ref:
                    false_chars += sum(1 for s in hyp if s != 1)
                    false_s += (x.shape[2] - net.delay * net.stride) * 0.005
                    continue
                b = "<-5" if snr < -5 else "-5..5" if snr < 5 else "5..15" if snr < 15 else ">=15"
                errs[b] = errs.get(b, 0) + lev(hyp, ref)
                chars[b] = chars.get(b, 0) + len(ref)
    net.train()
    cer = {b: errs[b] / max(1, chars[b]) for b in errs}
    return {"loss": tot_loss / max(1, n), "cer": sum(errs.values()) / max(1, sum(chars.values())), "by_snr": cer,
            "false_cpm": 60 * false_chars / max(1e-9, false_s)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=48)
    ap.add_argument("--dilations", default="1,2,4,8,16,32,64")
    ap.add_argument("--front", default="v1", help="shack_cw.nn.FRONTS: the features the network reads")
    ap.add_argument("--ahead", default="", help="dilations whose blocks look ahead (e.g. 1,2,4,8: 0.3 s)")
    ap.add_argument("--steps", type=int, default=40000)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--wpm-weight", type=float, default=0.05)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--eval-every", type=int, default=1000)
    ap.add_argument("--init", help="start from this checkpoint (.pt)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cache-only", type=int, metavar="JOBS",
                    help="only make every shard's feature file for --front, JOBS at once, and stop")
    a = ap.parse_args(argv)
    if a.cache_only:
        # About 14 ms an example: a set of a hundred thousand is half an hour
        # of one core, which a run would otherwise spend before its first step.
        import concurrent.futures as cf
        shards = [p for d in a.data + [a.val] for p in sorted(Path(d).expanduser().glob("shard-*.npz"))
                  if ".tmp" not in p.name]
        with cf.ProcessPoolExecutor(a.cache_only) as ex:
            for p in ex.map(feature_cache, shards, [a.front] * len(shards)):
                print(p.name, flush=True)
        return 0
    torch.set_num_threads(a.threads)
    torch.manual_seed(a.seed)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "log.jsonl", "a", encoding="utf-8")
    tr, val = Shards(a.data, a.front), Shards([a.val], a.front)
    print(f"train {len(tr)} examples, val {len(val)}", flush=True)
    net = M.CwNet(a.width, [int(d) for d in a.dilations.split(",")], front=a.front,
                  ahead=[int(d) for d in a.ahead.split(",") if d])
    if a.init:
        sd = torch.load(a.init)["model"]
        w = sd["down.weight"]
        if w.shape[1] < net.n_feat:
            # A front with more features than the checkpoint's (v1 -> v2):
            # every front begins with v1's features in v1's order, so the
            # extra inputs start at zero weight and the network starts out
            # exactly the one it was, then learns what the new ones add.
            sd["down.weight"] = torch.cat([w, w.new_zeros(w.shape[0], net.n_feat - w.shape[1], w.shape[2])], 1)
        net.load_state_dict(sd)
    print(f"params {net.params()}, front {net.front}, delay {net.delay} outputs", flush=True)
    opt = torch.optim.AdamW(net.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.steps,
                                                pct_start=min(0.3, max(0.05, 10 / a.steps)))
    rng = np.random.default_rng(a.seed)
    best = 9.9
    step, t0, run_loss = 0, time.time(), 0.0
    out_s = 0.005 * net.stride
    while step < a.steps:
        added = tr.refresh()
        if added:
            print(f"step {step}: +{added} examples, {len(tr)} in all", flush=True)
        perm = rng.permutation(len(tr))
        for b in range(0, len(perm) - a.batch + 1, a.batch):
            idx = np.sort(perm[b:b + a.batch])
            x, tgt, tl = tr.batch(idx, tail=net.delay * net.stride)
            lp, lw = net(x)
            T = lp.shape[2]
            ctc = F.ctc_loss(lp.permute(2, 0, 1), tgt, torch.full((len(idx),), T, dtype=torch.long), tl,
                             blank=BLANK, reduction="mean", zero_infinity=True)
            tg, mk = wpm_target(tr, idx, T, out_s, net.delay)
            wl = ((lw - tg) ** 2 * mk).sum() / mk.sum().clamp(min=1)
            loss = ctc + a.wpm_weight * wl
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
            sched.step()
            step += 1
            run_loss += ctc.item()
            if step % 100 == 0:
                print(f"step {step} ctc {run_loss / 100:.3f} wpm {float(wl):.3f} lr {sched.get_last_lr()[0]:.2e} "
                      f"{(time.time() - t0) / step:.2f} s/step", flush=True)
                run_loss = 0.0
            if step % a.eval_every == 0 or step == a.steps:
                ev = evaluate(net, val)
                ev.update(step=step, elapsed=time.time() - t0, train=len(tr))
                print(json.dumps(ev), flush=True)
                log.write(json.dumps(ev) + "\n")
                log.flush()
                ck = {"model": net.state_dict(), "config": net.config(), "step": step}
                torch.save(ck, out / "last.pt")
                torch.save(ck, out / f"step-{step:06d}.pt")      # for average.py
                if ev["cer"] < best:
                    best = ev["cer"]
                    torch.save({"model": net.state_dict(), "config": net.config(), "step": step}, out / "best.pt")
                    M.export(net, str(out / "best.npz"), {"step": step, "val_cer": ev["cer"], "trained": "synthetic"})
                    print(f"best {best:.4f}, export diff {M.check_export(net, str(out / 'best.npz')):.2e}", flush=True)
            if step >= a.steps:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
