"""A language model in the network's search: what callsigns and CW
phrases look like, weighed against what the network heard.

    dec = shack_cw.Decoder(freq=700.0, engine="nn", lm=0.5)     # beam search with the shipped model

The network decides each 20 ms output on its own, and the greedy reading
takes its best guess there and then: an E or T where a letter was half
heard, a space where a fist stretched a letter gap ("DE OE 7AJI"), none
where it shortened a word space ("CQDESM5D"). An operator copies the same
signal knowing what comes next: a call has a prefix, a digit and a short
suffix, DE is followed by a call, a call he heard last week is likelier
than one nobody holds. This is that knowledge in the decoder's search: a
CTC prefix beam search (several readings kept at once, merged where they
spell the same) scoring each reading as

    network log-probability + weight * LM log-probability + bonus per character
                            + known_call for each word that is a call on the list

The LM is a character n-gram (interpolated Kneser-Ney, order 6) over the
network's own alphabet, spaces included, built by tools/cw_lm/build.py
from REAL text only: the 15 s stretches of real bands where the classic
engine and the network printed the same thing (the CW survey, 14/10/2026),
with each call in them replaced by calls the RBN spotted on other days,
and those calls themselves. Round 2's space model (nn.space) was built
from synthetic overs and lost calls; it also overruled the network's
spaces one gap at a time, with no way back. Here nothing is decided until
the beams agree or LAG_S has passed, and a reading the network is sure of
costs the LM far more to overturn than one it is not.

The search is per channel and in Python, so it has to be frugal: an
output where only the blank is likely is skipped (it moves every beam
alike, so it changes no ranking; most of a channel's outputs are such),
and only symbols within PRUNE of certainty are tried. A character comes
out when every beam agrees on it, or LAG_S after it was heard at the
latest - the price of letting the next letters change it.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from ..i18n import _

TABLE = Path(__file__).with_name("cwlm.npz")

#: Chosen on the realval dev set and the CW survey's held-out lines
#: (tools/cw_lm/evaluate.py, 14/10/2026). The beam, the margin and the
#: prune moved the calls found by no more than the sets' noise; the
#: prune at -5 halved the search's CPU against -7. LAG_S 0.5 read as
#: well as 1 s and 2 s. A bonus for a word on the call list (KNOWN_CALL)
#: found no more calls and printed more listed calls in windows too weak
#: to read, so it is off: the list reaches the search through the
#: n-gram, which learnt the calls' spelling from it.
BEAM = 8
PRUNE = -5.0        # a symbol is tried only where its log-probability is over this
LAG_S = 0.5         # a character is printed at most this long after it was heard
KNOWN_CALL = 0.0    # added for each word that is a call on the model's list
MARGIN = 8.0        # a reading scored this far under the best is dropped


def _code(ctx) -> int:
    """A context's key: its symbols as digits base 64 (no symbol is 0, the
    blank, so contexts of different lengths never share a key)."""
    c = 0
    for s in ctx:
        c = (c << 6) | int(s)
    return c


class CharLM:
    """The n-gram table: for every context it keeps (up to order - 1
    symbols), the log-probability of each next symbol, already smoothed
    towards the shorter contexts, so a lookup is the longest context the
    table holds and nothing else."""

    def __init__(self, path: Path | str | None = None):
        from . import SYMBOLS
        z = np.load(Path(path) if path else TABLE, allow_pickle=False)
        self.meta = json.loads(str(z["meta"]))
        if self.meta["symbols"] != SYMBOLS:
            raise ValueError(_("this language model was built for another alphabet"))
        self.order = int(self.meta["order"])
        self.rows = np.asarray(z["logp"], np.float32)
        self.index = {int(c): i for i, c in enumerate(z["codes"])}
        self.calls = frozenset(bytes(z["calls"]).decode().split()) if "calls" in z.files else frozenset()
        self._dist: dict[tuple, np.ndarray] = {}

    def dist(self, ctx: tuple) -> np.ndarray:
        """Log-probabilities of the next symbol after `ctx` (symbol ids,
        oldest first; only the last order - 1 count)."""
        d = self._dist.get(ctx)
        if d is not None:
            return d
        for k in range(min(len(ctx), self.order - 1), -1, -1):
            i = self.index.get(_code(ctx[len(ctx) - k:]))
            if i is not None:
                d = self.rows[i]
                break
        if len(self._dist) > 100000:
            self._dist.clear()
        self._dist[ctx] = d
        return d

    def known(self, word: str) -> bool:
        """A word that is a call on the list, or one with a /P, /M, EA8/ ...
        around a call on it."""
        if word in self.calls:
            return True
        return "/" in word and any(p in self.calls for p in word.split("/") if len(p) >= 3)


_LM: dict[str, CharLM] = {}


def load(path: Path | str | None = None) -> CharLM:
    key = str(path or TABLE)
    if key not in _LM:
        _LM[key] = CharLM(path)
    return _LM[key]


def _lse(a: float, b: float) -> float:
    if a < b:
        a, b = b, a
    if b == -math.inf:
        return a
    return a + math.log1p(math.exp(b - a))


class _Beam:
    """One reading: the symbols since the last commit (with the output
    each was first heard at), CTC's two path probabilities (ending in a
    blank, ending in the last symbol), the LM's share of the score, and
    the LM's state (the last symbols, the word so far)."""
    __slots__ = ("syms", "ts", "pb", "pnb", "lm", "ctx", "word", "last")

    def __init__(self, syms, ts, pb, pnb, lm, ctx, word, last):
        self.syms, self.ts, self.pb, self.pnb, self.lm = syms, ts, pb, pnb, lm
        self.ctx, self.word, self.last = ctx, word, last

    def score(self) -> float:
        return _lse(self.pb, self.pnb) + self.lm


class Search:
    """One channel's streaming beam search. `step(logp, o0)` takes the
    network's next outputs (m, len(SYMBOLS)), the first of them output
    number o0, and returns the (output number, symbol) pairs it is now
    sure of, in order - what the greedy reading's `best` collapse gives,
    a little later and better spelt."""

    def __init__(self, lm: CharLM, weight: float, bonus: float = 0.0, beam: int = BEAM, prune: float = PRUNE,
                 lag: int = 25, known_call: float = KNOWN_CALL, margin: float = MARGIN):
        from . import SPACE, SYMBOLS
        self.lm, self.weight, self.bonus = lm, float(weight), float(bonus)
        self.beam, self.prune, self.lag, self.known_call = int(beam), float(prune), int(lag), float(known_call)
        self.margin = float(margin)
        self.space = SPACE
        self.chars = SYMBOLS
        ctx = (SPACE,) * (lm.order - 1)      # a channel starts as after a word
        self.beams = [_Beam((), (), 0.0, -math.inf, 0.0, ctx, "", -1)]
        self.blank_run = False

    def _extend(self, b: _Beam, c: int) -> tuple[float, tuple, str]:
        """The LM's score for reading `c` after beam b, and its new state."""
        lp = float(self.lm.dist(b.ctx)[c])
        sc = self.weight * lp + self.bonus
        if c == self.space:
            if b.word and self.known_call and self.lm.known(b.word):
                sc += self.known_call
            word = ""
        else:
            word = b.word + self.chars[c]
        return sc, b.ctx[1:] + (c,), word

    def step(self, logp: np.ndarray, o0: int) -> list[tuple[int, int]]:
        BLANK, SPACE = 0, self.space
        live = logp[:, 1:] > self.prune                              # (m, S-1): symbols worth trying
        busy = live.any(axis=1)
        out: list[tuple[int, int]] = []
        for i in range(len(logp)):
            # Commit output by output, not block by block, so what is
            # printed is the same however the outputs arrive.
            if not busy[i]:
                best = self.beams[0]
                if best.ts and best.ts[0] <= o0 + i + 1 - self.lag:
                    out += self._commit(o0 + i + 1)
                if self.blank_run:
                    # Every beam ends in a blank already: another blank
                    # adds the same to each and changes no ranking.
                    continue
                yb = float(logp[i, BLANK])
                for b in self.beams:
                    b.pb, b.pnb = _lse(b.pb, b.pnb) + yb, -math.inf
                self.blank_run = True
                continue
            self.blank_run = False
            y = logp[i].tolist()
            cand = (np.flatnonzero(live[i]) + 1).tolist()
            yb = y[BLANK]
            t = o0 + i
            nxt: dict[tuple, _Beam] = {}
            for b in self.beams:
                tot = _lse(b.pb, b.pnb)
                # The same reading: a blank, or the last symbol held on.
                same = nxt.get(b.syms)
                if same is None:
                    same = nxt[b.syms] = _Beam(b.syms, b.ts, -math.inf, -math.inf, b.lm, b.ctx, b.word, b.last)
                same.pb = _lse(same.pb, tot + yb)
                if b.last in cand:
                    same.pnb = _lse(same.pnb, b.pnb + y[b.last])
                for c in cand:
                    if c == SPACE and (b.last == SPACE or b.last == -1):
                        continue               # one space between words, none before the first
                    # A repeated symbol needs a blank between (CTC).
                    p = (b.pb if c == b.last else tot) + y[c]
                    if p == -math.inf:
                        continue
                    key = b.syms + (c,)
                    nb = nxt.get(key)
                    if nb is None:
                        sc, ctx, word = self._extend(b, c)
                        nxt[key] = _Beam(key, b.ts + (t,), -math.inf, p, b.lm + sc, ctx, word, c)
                    else:
                        nb.pnb = _lse(nb.pnb, p)
            beams = sorted(nxt.values(), key=_Beam.score, reverse=True)[:self.beam]
            # A reading this far behind the best is not coming back; letting
            # it go is what lets the beams agree, and print, early.
            floor = beams[0].score() - self.margin
            self.beams = [b for b in beams if b.score() >= floor]
            out += self._commit(t + 1)
        return out

    def _commit(self, now: int) -> list[tuple[int, int]]:
        """What every beam agrees on, and whatever of the best beam was
        heard `lag` outputs ago or more, as (output, symbol); the beams
        keep only what comes after it."""
        beams = self.beams
        best = beams[0]
        n = min(len(b.syms) for b in beams)
        k = 0
        while k < n and all(b.syms[k] == best.syms[k] for b in beams):
            k += 1
        while k < len(best.syms) and best.ts[k] <= now - self.lag:
            k += 1
        if not k:
            return []
        out = list(zip(best.ts[:k], best.syms[:k]))
        head = best.syms[:k]
        kept = [b for b in beams if b.syms[:k] == head]
        for b in kept:
            b.syms, b.ts = b.syms[k:], b.ts[k:]
        self.beams = kept
        return out

    def flush(self) -> list[tuple[int, int]]:
        """Everything the best beam holds (the end of a recording)."""
        best = self.beams[0]
        out = list(zip(best.ts, best.syms))
        self.beams = [_Beam((), (), best.pb, best.pnb, 0.0, best.ctx, best.word, best.last)]
        return out
