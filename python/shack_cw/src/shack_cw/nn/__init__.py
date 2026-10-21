"""The neural CW engine: marks and gaps to letters by a small streaming
network, behind the classic decoder's own front end and squelch.

    dec = shack_cw.Decoder(freq=700.0, engine="nn")     # same API as the classic
    for ev in dec.feed(samples, 8000): ...

Why (ohs-libs research/cw-decoder-bench, 13/10/2026): on 78 RBN-spotted
calls from real bands our rule-based decoder copied 16 calls as a word,
DeepCW (a CTC network, AGPL, not usable here) 24, and the skimmer's
per-call recovery put most lost calls down to wrong letters, not to the
front end. A network reads a fist the way an operator does - from the
shape of the whole letter and its neighbours - where the classic decoder
judges each mark and gap against thresholds learnt from the last few.
DeepCW decodes 10 s windows again and again (3.4 % of a core a channel);
this one is causal and streamed, so each 5 ms frame is computed once and
a skimmer runs every channel in one batch.

What is the same as the classic engine, on purpose:

* The front end: Decoder._sums (AFC, the Hann-weighted 5 ms frames at the
  tone and REF_OFFSETS), or the skimmer's filter bank (frontend.Gather),
  which makes the same sums for many channels at once.
* `amplitudes()`, at four coherent windows (m = 1, 2, 4, 8 frames) in
  place of the one the classic engine picks from the speed it has learnt:
  the network sees fast and slow keying through the window that suits it
  without having to learn the speed first. Each value is a log ratio to
  the noise beside the tone over the last second, so the receiver's AGC
  and gain do not matter.
* The squelch (Decoder._frame's statistic: the 75th percentile of the
  tone over its floor across 2.5 s, open at `snr_db`): no character is
  shown while it is shut, and the characters of the second it took to
  open are let through when it does, as the classic engine hears them
  again. On real noise (static, key clicks) a network with no squelch
  printed 1.5 characters a minute (DeepCW, the bench's quiet targets).

The network (the weights' own `meta` has the exact shape): the 12
features at 200 frames a second; a strided convolution to 50 a second;
residual causal dilated convolutions (receptive field about 5 s, enough
for a slow "0" and its gap); a CTC head over SYMBOLS and a speed head.
Trained on synthetic bands only (tools/cw_neural), never on the bench's
test clips. NumPy only at run time: desktop builds are frozen and boxes
have no GPU, so the weights are a small .npz we own (GPL like the rest).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..decoder import FRAME_S, REF_OFFSETS, Decoder, amplitudes

#: What the network prints. Index 0 is CTC's blank. Prosigns print as the
#: classic engine prints them (Decoder._lookup: prosigns first).
SYMBOLS = (["", " "] + [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(d) for d in range(10)]
           + ["/", "?", ".", ",", "<AR>", "<BT>", "<SK>", "<KN>"])
BLANK, SPACE = 0, 1
FLOOR_FRAMES = 200              # the floor: mean log noise beside the tone over 1 s
#: Added to the space's log-probability (Bank.space_bonus). Chosen on real
#: KiwiSDR recordings kept apart from the bench (tools/cw_neural/realval.py,
#: 13/10/2026): 1.0 copied 34 of 82 RBN calls as words against 32 with none
#: and 31 with 2.0; neutral on synthetic validation.
SPACE_BONUS = 1.0
#: The space model's weight (Bank.space_lm, nn.space): 0 leaves word
#: spaces to the network alone.
SPACE_LM = 0.0
#: The language model's weight (Bank.lm, nn.lm): 0 is the greedy reading.
#: 0.5 found the most RBN calls on held-out real recordings (survey: 128
#: of 341 as words against 106, dev set 56 of 128 against 49); 0.25 found
#: fewer, 1.0 fewer and changed more of the text both engines agreed on.
#: Off by default: it prints each character up to LAG_S (0.5 s) later,
#: which a skimmer does not notice and a live reader does.
LM = 0.0
#: Added for each character the beam search reads, against the LM's
#: pull towards fewer, likelier characters (with LM 0.5).
LM_BONUS = 1.0
LM_BEAM = 8
#: Added for a word that is a call on the LM's list (nn.lm.KNOWN_CALL).
LM_KNOWN_CALL = 0.0
#: One bin of the skimmer's filter bank (skim.frontend.BIN_HZ), repeated
#: here so this module need not import the skimmer.
_BIN_HZ = 23.4375

#: The front ends a network can be trained for, named by its weights'
#: meta["front"] ("v1" when absent: the first network's). Per front:
#: `windows`, the coherent windows (frames) at which the tone and the
#: quieter and louder sides are read (amplitudes() at each); `near`, extra
#: frequencies beside the tone (Hz), each read at `near_windows`.
#:
#: v2 (round 2, 14/10/2026) adds an 80 ms window and four bins, one and
#: two filter-bank bins either side of the tone. v1 sees the tone through
#: one narrow window centred wherever the AFC put it: a weak NCDXF beacon
#: with the AFC 10-15 Hz out loses 2.5-5 dB in the 40 ms window, and the
#: network cannot tell a mistuned weak tone from no tone. The near bins
#: are whole bins of the skimmer's bank so a skimmer channel reads them
#: exactly (its gather rounds every column of a channel alike).
FRONTS = {
    "v1": {"windows": (1, 2, 4, 8), "near": (), "near_windows": ()},
    "v2": {"windows": (1, 2, 4, 8, 16), "near": (-2 * _BIN_HZ, -_BIN_HZ, _BIN_HZ, 2 * _BIN_HZ),
           "near_windows": (4, 8, 16)},
}
WINDOWS = FRONTS["v1"]["windows"]


def n_feat(front: str = "v1") -> int:
    """How many features a frame has with this front end."""
    f = FRONTS[front]
    return 3 * len(f["windows"]) + len(f["near"]) * len(f["near_windows"])


def offsets(front: str = "v1") -> tuple:
    """The frequencies (Hz from the tone) a front end sums for this front:
    REF_OFFSETS, then the near bins."""
    return tuple(REF_OFFSETS) + tuple(FRONTS[front]["near"])


N_FEAT = n_feat("v1")
WEIGHTS = Path(__file__).with_name("cwnn.npz")


def tokens(text: str) -> list[int]:
    """SYMBOLS indices for a text, prosigns written <XX>; other characters
    are dropped, runs of spaces are one."""
    out: list[int] = []
    i, up = 0, text.upper()
    alias = {"+": "<AR>", "=": "<BT>"}
    while i < len(up):
        ch = up[i]
        if ch == "<":
            j = up.find(">", i)
            tok = up[i:j + 1] if j > 0 else ""
            i = j + 1 if j > 0 else i + 1
        else:
            tok = alias.get(ch, ch)
            i += 1
        if tok == " ":
            if out and out[-1] != SPACE:
                out.append(SPACE)
        elif tok in SYMBOLS and tok:
            out.append(SYMBOLS.index(tok))
    while out and out[-1] == SPACE:
        out.pop()
    return out


def _med3(a, b, c):
    return np.maximum(np.minimum(a, b), np.minimum(np.maximum(a, b), c))


class Features:
    """Complex sums (frames, K, columns) -> features (frames, K,
    n_feat(front)), float32. The columns are offsets(front): the tone,
    REF_OFFSETS, then the front's near bins; extra columns are ignored, so
    sums made for v2 serve a v1 network too.

    Per window m: the tone, the quieter side and the louder side, exactly
    as amplitudes() makes them; then each near bin at each of the front's
    near windows, the same way (a coherent sum over m frames, its
    amplitude averaged over m). All as natural logs over the floor - the
    mean log of the quieter side at m = 4 over the last FLOOR_FRAMES
    (fewer at a stream's start). Every step runs on across calls, so a
    stream cut into blocks of any length gives the same numbers as one
    long call: the network is trained on whole examples and runs on 75 ms
    blocks. A channel starts with a zero history, as the skimmer's pool
    starts its channels, so a decoder's first frames and a skimmer
    channel's are the same.

    The windows are done at once here rather than by calling amplitudes()
    once a window: that was a third of the engine's CPU in one decoder,
    all of it numpy's per-call overhead on a few frames. The numbers are
    amplitudes()' to rounding (test_nn checks it)."""

    def __init__(self, k: int = 0, front: str = "v1"):
        f = FRONTS[front]
        self.front = front
        self.M = np.array(f["windows"])
        self.HIST = int(self.M.max())
        self.cols = len(offsets(front))
        # Which windows the near bins are read at, as indices into M.
        self.near_w = [list(f["windows"]).index(m) for m in f["near_windows"]]
        self.q4 = 3 * list(f["windows"]).index(4) + 1
        self.n_feat = n_feat(front)
        self.chist = np.zeros((self.HIST, k, self.cols), np.complex128)    # the last frames' sums
        self.ahist = np.zeros((len(self.M), self.HIST, k, self.cols))      # each window's last amplitudes
        self.lq = np.zeros((FLOOR_FRAMES, k))     # log quiet, newest last; zeros before a channel began
        self.seen = np.zeros(k, np.int64)

    def __call__(self, c: np.ndarray, scale: float = 1.0) -> np.ndarray:
        n, k = c.shape[:2]
        h, C, M = self.HIST, self.cols, self.M
        cc = np.concatenate([self.chist, c[..., :C]])
        cs = np.concatenate([np.zeros((1, k, C), np.complex128), np.cumsum(cc, axis=0)])
        j = np.arange(h, h + n) + 1
        # amplitudes(): the coherent sum over m frames, then the mean of
        # that amplitude over m frames, for every window at once.
        amp = np.abs(cs[j][None] - cs[j[None, :] - M[:, None]]) / (M[:, None, None, None] * scale)
        aa = np.concatenate([self.ahist, amp], axis=1)
        sa = np.concatenate([np.zeros((len(M), 1, k, C)), np.cumsum(aa, axis=1)], axis=1)
        w = np.arange(len(M))[:, None]
        sm = (sa[:, j] - sa[w, j[None, :] - M[:, None]]) / M[:, None, None, None]     # (W, n, K, C)
        lo = _med3(sm[..., 1], sm[..., 2], sm[..., 3])
        hi = _med3(sm[..., 4], sm[..., 5], sm[..., 6])
        a = np.stack([sm[..., 0], np.minimum(lo, hi), np.maximum(lo, hi)], axis=-1)  # (W, n, K, 3)
        a = a.transpose(1, 2, 0, 3).reshape(n, k, 3 * len(M))
        if self.near_w:
            near = sm[self.near_w][..., 7:]                                      # (Wn, n, K, near)
            a = np.concatenate([a, near.transpose(1, 2, 0, 3).reshape(n, k, -1)], axis=2)
        a = np.log(a + 1e-20)
        self.chist, self.ahist = cc[-h:], aa[:, -h:]
        lq = a[:, :, self.q4]
        allq = np.concatenate([self.lq, lq])
        cq = np.concatenate([np.zeros((1, k)), np.cumsum(allq, axis=0)])
        jj = np.arange(FLOOR_FRAMES, FLOOR_FRAMES + n) + 1
        # Entries from before a channel began are zeros, so the window's sum
        # is the sum of what it has seen; divided by how many that is.
        cnt = np.minimum(self.seen[None, :] + np.arange(1, n + 1)[:, None], FLOOR_FRAMES)
        floor = (cq[jj] - cq[jj - FLOOR_FRAMES]) / cnt
        self.lq = allq[-FLOOR_FRAMES:]
        self.seen = self.seen + n
        return (a - floor[:, :, None]).astype(np.float32)

    def select(self, keep: np.ndarray) -> None:
        """Keep only the channels `keep` (indices), in that order."""
        self.chist, self.ahist = self.chist[:, keep], self.ahist[:, :, keep]
        self.lq = self.lq[:, keep]
        self.seen = self.seen[keep]

    def grow(self, n: int) -> None:
        """Add `n` channels at the end, started afresh."""
        self.chist = np.concatenate([self.chist, np.zeros((self.HIST, n, self.cols), np.complex128)], 1)
        self.ahist = np.concatenate([self.ahist, np.zeros((len(self.M), self.HIST, n, self.cols))], 2)
        self.lq = np.concatenate([self.lq, np.zeros((FLOOR_FRAMES, n))], 1)
        self.seen = np.concatenate([self.seen, np.zeros(n, np.int64)])


class Net:
    """The network, streamed: each call takes the next frames of every
    channel and returns the outputs those frames complete, keeping just
    enough of each layer's past (2 x dilation frames) to carry on.

    A layer marked in meta["ahead"] looks ahead: its taps are t - d, t and
    t + d rather than t - 2d, t - d and t. Streamed, that is the same
    causal arithmetic with the residual taken from d frames back, so the
    layer's output is the one for d frames ago, and the network's outputs
    come `delay` outputs (the sum of those d) after the frames they
    describe. A letter is then judged with the gap after it and the start
    of the next one in view, where a causal network has to guess whether
    a gap is a letter's or a word's. It costs latency (0.3 s for d = 1, 2,
    4, 8), not arithmetic."""

    def __init__(self, path: Path | str | None = None):
        z = np.load(Path(path) if path else WEIGHTS, allow_pickle=False)
        self.meta = json.loads(str(z["meta"]))
        m = self.meta
        self.front = m.get("front", "v1")
        if (m["symbols"] != SYMBOLS or self.front not in FRONTS or m["n_feat"] != n_feat(self.front)
                or list(m["windows"]) != list(FRONTS[self.front]["windows"])):
            raise ValueError("these weights were trained for another front end or alphabet")
        self.n_feat = n_feat(self.front)
        self.stride, self.kd = int(m["stride"]), int(m["k_down"])
        self.dil = [int(d) for d in m["dilations"]]
        self.ahead = [bool(a) for a in m.get("ahead", [False] * len(self.dil))]
        self.delay = sum(d for d, a in zip(self.dil, self.ahead) if a)      # in outputs
        f32 = lambda k: np.ascontiguousarray(z[k], np.float32)
        self.down_w, self.down_b = f32("down_w"), f32("down_b")
        self.conv_w = [f32(f"conv{i}_w") for i in range(len(self.dil))]
        self.conv_b = [f32(f"conv{i}_b") for i in range(len(self.dil))]
        self.mix_w = [f32(f"mix{i}_w") for i in range(len(self.dil))]
        self.mix_b = [f32(f"mix{i}_b") for i in range(len(self.dil))]
        self.out_w, self.out_b = f32("out_w"), f32("out_b")
        self.wpm_w, self.wpm_b = f32("wpm_w"), f32("wpm_b")
        self.width = self.down_b.shape[0]
        self.params = int(sum(z[k].size for k in z.files if k != "meta"))

    def state(self, k: int) -> dict:
        """A fresh stream for `k` channels: the left padding is zeros, as
        the network was trained."""
        return {"in": np.zeros((k, self.kd - self.stride, self.n_feat), np.float32),
                "x": [np.zeros((k, 2 * d, self.width), np.float32) for d in self.dil]}

    @staticmethod
    def select(st: dict, keep: np.ndarray) -> dict:
        return {"in": st["in"][keep], "x": [x[keep] for x in st["x"]]}

    def join(self, st: dict, n: int) -> dict:
        """`st` with `n` fresh channels added at the end. A new channel's
        pending input is silence up to the bank's stride phase, so its
        outputs fall when everyone else's do."""
        fresh = self.state(n)
        fresh["in"] = np.zeros((n,) + st["in"].shape[1:], np.float32)
        return {"in": np.concatenate([st["in"], fresh["in"]]),
                "x": [np.concatenate([a, b]) for a, b in zip(st["x"], fresh["x"])]}

    def step(self, feat: np.ndarray, st: dict) -> tuple[np.ndarray, np.ndarray]:
        """feat (K, n, n_feat) -> (log-probabilities (K, m, len(SYMBOLS)),
        speed in WPM (K, m)) for the m outputs these frames complete."""
        buf = np.concatenate([st["in"], feat.astype(np.float32)], axis=1)
        k = len(buf)
        m = (buf.shape[1] - self.kd) // self.stride + 1 if buf.shape[1] >= self.kd else 0
        st["in"] = buf[:, m * self.stride:]
        if m <= 0:
            return np.zeros((k, 0, len(SYMBOLS)), np.float32), np.zeros((k, 0), np.float32)
        win = np.lib.stride_tricks.sliding_window_view(buf, self.kd, axis=1)[:, ::self.stride][:, :m]
        # (K, m, F, kd) -> (K, m, kd*F): tap-major, as the weights are laid out.
        x = win.transpose(0, 1, 3, 2).reshape(k, m, -1) @ self.down_w + self.down_b
        np.maximum(x, 0, out=x)
        for i, d in enumerate(self.dil):
            xx = np.concatenate([st["x"][i], x], axis=1)
            st["x"][i] = xx[:, -2 * d:]
            taps = np.concatenate([xx[:, :m], xx[:, d:d + m], xx[:, 2 * d:2 * d + m]], axis=2)
            h = taps @ self.conv_w[i] + self.conv_b[i]
            np.maximum(h, 0, out=h)
            # A look-ahead layer's output is for its middle tap's frame.
            x = (xx[:, d:d + m] if self.ahead[i] else x) + (h @ self.mix_w[i] + self.mix_b[i])
        logits = x @ self.out_w + self.out_b
        logits -= logits.max(axis=-1, keepdims=True)
        logp = logits - np.log(np.exp(logits).sum(axis=-1, keepdims=True))
        wpm = 20.0 * np.exp((x @ self.wpm_w + self.wpm_b)[..., 0])
        return logp, wpm


class Ensemble:
    """Several networks run as one: Net's interface, their probabilities
    averaged output by output. Networks trained apart make different
    mistakes, and the average of what they believe is wrong less often
    than any one of them, for the cost of running each (the network is
    about half of a skimmer channel's CPU; the front end and the squelch
    are shared). Their fronts may differ - every front starts with v1's
    features, so each network reads the first n_feat of the richest - but
    their stride and look-ahead must match, or their outputs would not
    line up."""

    def __init__(self, nets: list[Net]):
        self.nets = nets
        if len({(n.stride, n.delay) for n in nets}) != 1:
            raise ValueError("an ensemble's networks must share their stride and look-ahead")
        top = max(nets, key=lambda n: n.n_feat)
        self.front, self.n_feat = top.front, top.n_feat
        self.stride, self.delay = nets[0].stride, nets[0].delay
        self.params = sum(n.params for n in nets)
        self.meta = {"ensemble": [n.meta for n in nets]}

    def state(self, k: int) -> dict:
        return {"nets": [n.state(k) for n in self.nets]}

    def select(self, st: dict, keep: np.ndarray) -> dict:
        return {"nets": [n.select(s, keep) for n, s in zip(self.nets, st["nets"])]}

    def join(self, st: dict, n: int) -> dict:
        return {"nets": [m.join(s, n) for m, s in zip(self.nets, st["nets"])]}

    def step(self, feat: np.ndarray, st: dict) -> tuple[np.ndarray, np.ndarray]:
        outs = [n.step(feat[..., :n.n_feat], s) for n, s in zip(self.nets, st["nets"])]
        lp = np.stack([o[0] for o in outs])
        top = lp.max(axis=0)
        logp = top + np.log(np.exp(lp - top).mean(axis=0))
        return logp.astype(np.float32), np.mean([o[1] for o in outs], axis=0)


_NET: dict[str, Net | Ensemble] = {}


def load(path: Path | str | None = None) -> Net | Ensemble:
    """The network, loaded once per process (every decoder shares it).
    Several weights files joined by "+" are an Ensemble."""
    key = str(path or WEIGHTS)
    if key not in _NET:
        parts = key.split("+")
        _NET[key] = Net(parts[0]) if len(parts) == 1 else Ensemble([load(p) for p in parts])
    return _NET[key]


class Squelch:
    """The classic engine's squelch (Decoder._frame), vectorised over
    channels and run at the network's 50 frames a second: the tone at
    m = 4 over a floor (the quieter side, smoothed, times a ratio learnt as
    a running 30th percentile of tone over side), its 75th percentile over
    the last 2.5 s, open at `snr_db` and shut 1 dB below."""

    WINDOW_S = Decoder.SQ_WINDOW_S
    HYST_DB = Decoder.SQ_HYST_DB

    def __init__(self, k: int, snr_db: float, frame_s: float):
        self.snr_db = snr_db
        self.n = int(round(self.WINDOW_S / frame_s))
        self.rate = 1.0 / frame_s
        self.reset(k)

    def reset(self, k: int) -> None:
        self.ref = np.full(k, np.nan)
        self.ratio = np.ones(k)
        self.learnt = np.zeros(k, np.int64)
        self.ring = np.zeros((k, self.n))
        self.filled = np.zeros(k, np.int64)
        self.pos = 0
        self.stat = np.zeros(k)
        self.open = np.zeros(k, bool)
        self.count = 0

    def select(self, keep) -> None:
        for name in ("ref", "ratio", "learnt", "ring", "filled", "stat", "open"):
            setattr(self, name, getattr(self, name)[keep])

    def grow(self, n: int) -> None:
        self.ref = np.concatenate([self.ref, np.full(n, np.nan)])
        self.ratio = np.concatenate([self.ratio, np.ones(n)])
        self.learnt = np.concatenate([self.learnt, np.zeros(n, np.int64)])
        self.ring = np.concatenate([self.ring, np.zeros((n, self.n))])
        self.filled = np.concatenate([self.filled, np.zeros(n, np.int64)])
        self.stat = np.concatenate([self.stat, np.zeros(n)])
        self.open = np.concatenate([self.open, np.zeros(n, bool)])

    def step(self, tone: np.ndarray, quiet: np.ndarray) -> np.ndarray:
        """tone, quiet (m, K) linear amplitudes -> open (m, K)."""
        out = np.zeros(tone.shape, bool)
        on = 10 ** (self.snr_db / 20)
        off = on * 10 ** (-self.HYST_DB / 20)
        for i in range(len(tone)):
            e, q = tone[i], quiet[i]
            self.ref = np.where(np.isnan(self.ref), q, self.ref + 0.35 * (q - self.ref))
            fl = self.ratio * Decoder.RATIO_C * self.ref
            r = np.where(fl > 0, e / np.where(fl > 0, fl, 1.0), 0.0)
            self.ring[:, self.pos] = r
            self.filled = np.minimum(self.filled + 1, self.n)
            # The classic engine's learning (Decoder._step), at a quarter of
            # its frame rate so four times the step: 1 % then 0.3 % a frame.
            step = np.where(self.learnt < 100, np.log(1.04), np.log(1.012))
            up = e > self.ratio * self.ref
            self.ratio *= np.exp(step * np.where(up, 2 * Decoder.RATIO_Q, -2 * (1 - Decoder.RATIO_Q)))
            self.learnt += 1
            self.pos = (self.pos + 1) % self.n
            self.count += 1
            if self.count >= 4:              # every 80 ms, as the classic's 75
                self.count = 0
                full = self.filled >= 10
                if full.any():
                    # The 75th percentile by partition: np.percentile's
                    # interpolation cost more than the rest of the squelch.
                    kth = int(round(Decoder.SQ_PCT / 100 * (self.n - 1)))
                    self.stat = np.where(full, np.partition(self.ring, kth, axis=1)[:, kth], 0.0)
            self.open = np.where(self.stat >= on, True, np.where(self.stat < off, False, self.open))
            out[i] = self.open
        return out


class Bank:
    """Many channels through one network call a block: the skimmer's pool
    and a single NnDecoder (a bank of one) both run this.

    Channels are named by any hashable key. `run(c, keys)` takes the
    complex sums (frames, len(keys), len(self.offsets)) and returns {key:
    [characters]}. A key not seen before starts afresh; a key missing from
    `keys` is dropped and its state forgotten. Every channel shares the
    bank's frame clock, so the network's stride and the squelch tick
    together for all.

    A look-ahead network's characters come Net.delay outputs late; the
    squelch is not delayed to match. It opens and shuts on 2.5 s of
    statistics, and a character it has not yet opened for waits HOLD_S, so
    the half second at most changes when nothing is let through."""

    HOLD_S = 2.5     # characters heard while the squelch was shut wait this long for it to open

    def __init__(self, snr_db: float = 6.0, weights: Path | str | None = None):
        self.net = load(weights)
        self.out_s = FRAME_S * self.net.stride
        #: The frequencies (Hz from each channel's tone) whose sums run() wants.
        self.offsets = offsets(self.net.front)
        self.keys: list = []
        self.feat = Features(0, self.net.front)
        self.st = self.net.state(0)
        self.sq = Squelch(0, snr_db, self.out_s)
        self.prev = np.zeros(0, np.int64)      # each channel's last output symbol (CTC's collapse)
        self.wpm = np.zeros(0)
        self.held: list[list] = []             # per channel: (output frame, symbol) waiting on the squelch
        self.last: list[str] = []              # per channel: the last character let out
        self.frames = 0                        # input frames since the bank started
        self.outs = 0                          # network outputs since the bank started
        # Added to the space's log-probability before the greedy choice:
        # word spaces are what real fists keep least, and a call run onto
        # its neighbour matches no pattern (chosen on validation, val.py).
        self.space_bonus = SPACE_BONUS
        # The space model's weight (nn.space; 0: the network's spaces alone).
        self.space_lm = SPACE_LM
        self.space_table = None                # its table (None: nn/spacelm.npz)
        # The language model's weight (nn.lm; 0: the greedy reading above).
        # Over 0 each channel's characters come from a beam search that
        # weighs the network against what CW text looks like.
        self.lm = LM
        self.lm_table = None                   # its table (None: nn/cwlm.npz)
        self.lm_bonus = LM_BONUS               # added per character the search reads
        self.lm_beam = LM_BEAM
        self.lm_known_call = LM_KNOWN_CALL
        self.lm_opts: dict = {}                # the search's other settings (nn.lm.Search; lag_s for LAG_S)
        self.search: list = []                 # per channel: its nn.lm.Search, made on first use
        self.trace: list | None = None         # (key, output, character) for each one let out, if a list
        self._space_model = None
        self.hist: list[list[int]] = []        # per channel: the last three symbols, for the space model
        self.gap = np.zeros(0)                 # per channel: the best space margin since its last character

    @property
    def space_model(self):
        if self._space_model is None:
            from .space import load as load_space
            self._space_model = load_space(self.space_table)
        return self._space_model

    @property
    def snr_db(self) -> float:
        return self.sq.snr_db

    @snr_db.setter
    def snr_db(self, v: float) -> None:
        self.sq.snr_db = float(v)

    def _take(self, idx: np.ndarray) -> None:
        self.feat.select(idx)
        self.st = self.net.select(self.st, idx)
        self.sq.select(idx)
        self.prev, self.wpm = self.prev[idx], self.wpm[idx]
        self.held = [self.held[i] for i in idx]
        self.last = [self.last[i] for i in idx]
        self.hist = [self.hist[i] for i in idx]
        self.search = [self.search[i] for i in idx]
        self.gap = self.gap[idx]

    def _sync(self, keys: list) -> None:
        if keys == self.keys:
            return
        old = {k: i for i, k in enumerate(self.keys)}
        kept = [k for k in self.keys if k in set(keys)]
        self._take(np.array([old[k] for k in kept], np.int64))
        new = [k for k in keys if k not in old]
        if new:
            n = len(new)
            self.feat.grow(n)
            self.st = self.net.join(self.st, n)
            self.sq.grow(n)
            self.prev = np.concatenate([self.prev, np.zeros(n, np.int64)])
            self.wpm = np.concatenate([self.wpm, np.zeros(n)])
            self.held += [[] for _ in new]
            self.last += ["" for _ in new]
            self.hist += [[] for _ in new]
            self.search += [None for _ in new]
            self.gap = np.concatenate([self.gap, np.full(n, -np.inf)])
        order = kept + new
        if order != keys:
            pos = {k: i for i, k in enumerate(order)}
            self._take(np.array([pos[k] for k in keys], np.int64))
        self.keys = list(keys)

    def run(self, c: np.ndarray, keys: list, scale: float = 1.0) -> dict:
        self._sync(list(keys))
        out: dict = {k: [] for k in self.keys}
        n = len(c)
        if not n or not self.keys:
            return out
        logp, wpm, opened = self._outputs(c, scale)
        if logp.shape[1]:
            self._read(logp, wpm, opened, out)
        return out

    def _outputs(self, c: np.ndarray, scale: float):
        """The network's outputs for these sums: log-probabilities (K, m,
        len(SYMBOLS)), speed (K, m), and whether the squelch is open at
        each (m, K)."""
        f = self.feat(c, scale)                                     # (n, K, F)
        # The squelch reads the frames that complete a network output (each
        # stride's last), so it ticks with the outputs.
        n = len(c)
        idx = np.arange(self.frames, self.frames + n)
        sel = np.where(idx % self.net.stride == self.net.stride - 1)[0]
        self.frames += n
        logp, wpm = self.net.step(f.transpose(1, 0, 2), self.st)  # (K, m, S), (K, m)
        m = logp.shape[1]
        if not m:
            return logp, wpm, np.zeros((0, len(self.keys)), bool)
        w4 = self.feat.q4 - 1                                       # the tone at m = 4
        a = np.exp(f[sel[:m]])                                      # amplitudes over the floor, (m, K, F)
        opened = self.sq.step(a[:, :, w4], a[:, :, w4 + 1])         # (m, K)
        return logp, wpm, opened

    def _read(self, logp: np.ndarray, wpm: np.ndarray, opened: np.ndarray, out: dict) -> None:
        """The network's outputs to characters, into out[key]: the greedy
        reading (with the space model if space_lm), or the beam search with
        the language model if lm (nn.lm); then the squelch lets them out."""
        m = logp.shape[1]
        if self.space_bonus:
            logp[..., SPACE] += self.space_bonus
        beam = self.lm > 0
        if self.space_lm and not beam:
            # How far a space beats everything else in each output (> 0:
            # the greedy choice). Without the space model a gap gets a space
            # if any of its outputs is one; with it, if the best of them
            # plus space_lm times the model's log-odds is over zero.
            other = logp.copy()
            other[..., SPACE] = -np.inf
            margin = logp[..., SPACE] - other.max(axis=-1)          # (K, m)
        best = logp.argmax(axis=-1)                                 # (K, m)
        o0, self.outs = self.outs, self.outs + m
        hold = int(round(self.HOLD_S / self.out_s))
        prev = np.concatenate([self.prev[:, None], best[:, :-1]], axis=1)
        new = (best != prev) & (best != BLANK)
        self.prev = best[:, -1].copy()
        for j, k in enumerate(self.keys):
            if opened[:, j].any():
                w = float(np.median(wpm[j][opened[:, j]]))
                self.wpm[j] = w if self.wpm[j] <= 0 else self.wpm[j] + 0.2 * (w - self.wpm[j])
            held = self.held[j]
            if beam:
                if self.search[j] is None:
                    self.search[j] = self._new_search()
                held += self.search[j].step(logp[j], o0)
            elif self.space_lm:
                self._spaced(j, best[j], margin[j], np.flatnonzero(new[j]), o0)
            else:
                held += [(o0 + int(t), int(best[j, t])) for t in np.flatnonzero(new[j])]
            if not held:
                continue
            # Let out what the squelch is open for, at or after it was
            # heard; drop what has waited HOLD_S with the squelch shut.
            wait = []
            for t, sym in held:
                if opened[max(0, t - o0):, j].any():
                    ch = self._char(j, sym)
                    if ch:
                        out[k].append(ch)
                        if self.trace is not None:
                            self.trace.append((k, t, ch))
                elif o0 + m - 1 - t < hold:
                    wait.append((t, sym))
            self.held[j] = wait

    def _new_search(self):
        from .lm import LAG_S, Search, load as load_lm
        opts = dict(self.lm_opts)
        lag = int(round(opts.pop("lag_s", LAG_S) / self.out_s))
        return Search(load_lm(self.lm_table), self.lm, self.lm_bonus, beam=self.lm_beam, lag=lag,
                      known_call=self.lm_known_call, **opts)

    def _spaced(self, j: int, best: np.ndarray, margin: np.ndarray, ts: np.ndarray, o0: int) -> None:
        """Channel j's new symbols into its held list, each word space
        decided when the character after it comes out: the gap's best
        margin for a space, plus space_lm times the space model's log-odds
        for the last three symbols and that character. The network's own
        spaces are only evidence; a space is printed a character late."""
        held, hist = self.held[j], self.hist[j]
        g, a = self.gap[j], 0
        for t in ts:
            s = int(best[t])
            if s == SPACE:
                continue
            if t > a:
                g = max(g, float(margin[a:t].max()))
            if hist and hist[-1] != SPACE and g + self.space_lm * self.space_model.logodds(hist, s) > 0:
                held.append((o0 + int(t), SPACE))
                hist.append(SPACE)
            held.append((o0 + int(t), s))
            hist.append(s)
            del hist[:-3]
            g, a = -np.inf, int(t) + 1
        if a < len(margin):
            g = max(g, float(margin[a:].max()))
        self.gap[j] = g

    def _char(self, j: int, sym: int) -> str:
        ch = SYMBOLS[sym]
        if ch == " " and (not self.last[j] or self.last[j] == " "):
            return ""
        self.last[j] = ch
        return ch

    def hearing(self, j: int = 0) -> bool:
        return bool(self.sq.open[j]) if len(self.sq.open) > j else False

    def snr(self, j: int = 0) -> float | None:
        s = self.sq.stat[j] if len(self.sq.stat) > j else 0.0
        return round(20 * float(np.log10(s)), 1) if s > 0 else None


class NnDecoder(Decoder):
    """Decoder(engine="nn"): the classic decoder's front end (mixer, AFC,
    frames), then the network in place of its levels and timing. Same
    events, same properties; `feed_frames` is the classic engine's alone
    (the network needs every reference, not three amplitudes): a skimmer
    runs a Bank over its channels' sums instead (skim.pool)."""

    def __init__(self, emit=None, freq: float = 700.0, wpm_hint: float = 20.0, squelch: float = 4.0, afc: bool = True,
                 snr_db: float = 6.0, polarity: bool = False, weights=None, space_bonus: float | None = None,
                 space_lm: float | None = None, space_table=None, lm: float | None = None, lm_table=None,
                 lm_bonus: float | None = None, lm_beam: int | None = None, lm_known_call: float | None = None,
                 **_opts):
        self._weights = weights
        self.space_bonus = SPACE_BONUS if space_bonus is None else space_bonus   # Bank.space_bonus; may change while running
        self.space_lm = SPACE_LM if space_lm is None else space_lm               # Bank.space_lm, likewise
        self._space_table = space_table
        # The language model's search (Bank.lm and its settings): fixed for
        # the decoder's life, since a channel's beams hold the model's state.
        self._lm = {"lm": LM if lm is None else float(lm), "lm_table": lm_table,
                    "lm_bonus": LM_BONUS if lm_bonus is None else float(lm_bonus),
                    "lm_beam": LM_BEAM if lm_beam is None else int(lm_beam),
                    "lm_known_call": LM_KNOWN_CALL if lm_known_call is None else float(lm_known_call)}
        super().__init__(emit, freq=freq, wpm_hint=wpm_hint, squelch=squelch, afc=afc, snr_db=snr_db,
                         polarity=polarity)

    def reset(self) -> None:
        self._bank = Bank(self.snr_db, self._weights)
        self._bank.space_table = self._space_table
        for k, v in self._lm.items():
            setattr(self._bank, k, v)
        # The front end sums whatever frequencies these weights read
        # (offsets(front)); set before the classic reset sizes its mixers.
        self._ref_off = np.array(self._bank.offsets)
        super().reset()
        self._pending: list[np.ndarray] = []

    #: Frames gathered before the network runs (150 ms: two of an app's
    #: 75 ms blocks). One channel pays numpy's per-call overhead in every
    #: layer each time; every other block halves it, for a character
    #: shown at most 75 ms later.
    GATHER = 30

    def _from_sums(self, c: np.ndarray) -> None:
        self._pending.append(c)
        if sum(len(x) for x in self._pending) < self.GATHER:
            return
        c = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        self._pending = []
        self._bank.snr_db = self.snr_db
        self._bank.space_bonus = self.space_bonus
        self._bank.space_lm = self.space_lm
        chars = self._bank.run(c[:, None, :], [0], scale=self.frame_len)[0]
        if self._bank.wpm[0] > 0:
            self._dot = 1.2 / float(self._bank.wpm[0])
        for ch in chars:
            self._emit(ch)

    def feed_frames(self, tone, quiet, loud=None):
        raise NotImplementedError("the neural engine reads the complex sums: run a shack_cw.nn.Bank")

    @property
    def wpm(self) -> float | None:
        # The bank has no channel until the first GATHER frames have run
        # (150 ms): an app asking before then gets "not yet", not an error.
        w = self._bank.wpm
        return round(float(w[0]), 1) if len(w) and w[0] > 0 else None

    @property
    def locked(self) -> bool:
        return bool(len(self._bank.wpm) and self._bank.wpm[0] > 0)

    @property
    def snr(self) -> float | None:
        return self._bank.snr(0)

    @property
    def hearing(self) -> bool:
        return self._bank.hearing(0)


class _Collect(Decoder):
    """The classic front end alone: each block's complex sums at `offs`, kept."""

    def __init__(self, *a, offs=REF_OFFSETS, **kw):
        self._offs = np.array(offs)
        super().__init__(*a, **kw)

    def reset(self) -> None:
        self._ref_off = self._offs
        super().reset()
        self.sums: list[np.ndarray] = []

    def _from_sums(self, c: np.ndarray) -> None:
        self.sums.append(c)


def front_sums(audio: np.ndarray, rate: float, freq: float = 700.0, afc: bool = True, block_s: float = 0.075,
               front: str = "v1") -> np.ndarray:
    """An audio stream through the decoder's own front end (AFC and the
    5 ms frames) in blocks of `block_s`, as an app feeds it: the complex
    sums (frames, len(offsets(front))), divided by the frame length as
    Bank.run's `scale` divides them. Training keeps these and makes any
    front's features from them (Features), so one set of examples serves
    every front end whose columns it holds."""
    offs = offsets(front)
    d = _Collect(None, freq=freq, afc=afc, offs=offs)
    blk = max(1, int(round(block_s * rate)))
    for i in range(0, len(audio), blk):
        d.feed(audio[i:i + blk], rate)
    if not d.sums:
        return np.zeros((0, len(offs)), np.complex128)
    return np.concatenate(d.sums) / d.frame_len


def front_end(audio: np.ndarray, rate: float, freq: float = 700.0, afc: bool = True,
              block_s: float = 0.075, front: str = "v1") -> np.ndarray:
    """front_sums, then the network's features (frames, n_feat(front)):
    what training computes for every example, so the network learns from
    exactly what it will be given."""
    c = front_sums(audio, rate, freq, afc, block_s, front)
    return Features(1, front)(c[:, None, :])[:, 0, :]
