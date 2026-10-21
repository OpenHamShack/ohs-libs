"""The space model: whether a word space belongs between what was printed
and the next character, from the shape of the language alone.

A CTC network decides each word space from the gap in front of it, and
real fists make that the least reliable thing they send: "CQDE" run
together, "DK5L M" pulled apart (round 1 lost most of its calls that way).
An operator settles it from the text: there is a space after CQ and DE,
none between a call's prefix and its digit. This is that knowledge as a
table - the log-odds of a space given the last three symbols printed
(spaces included) and the next character, backed off to two, one and
none - built from synthetic overs by tools/cw_neural/spacelm.py, so it
holds no real station's text. Bank adds `weight` times it to the
network's own evidence for a space in that gap (Bank.space_lm).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

TABLE = Path(__file__).with_name("spacelm.npz")


def _n() -> int:
    from . import SYMBOLS
    return len(SYMBOLS)


N = 46       # len(SYMBOLS); checked on load


def code(hist, nxt: int) -> int:
    """The context's key: the history symbols (oldest first) then the next
    character, as digits base N, so dropping the oldest is `% N ** k`."""
    c = 0
    for s in hist:
        c = c * N + int(s)
    return c * N + int(nxt)


class SpaceModel:
    def __init__(self, path: Path | str | None = None):
        if N != _n():
            raise ValueError("the space model's alphabet is not the network's")
        z = np.load(Path(path) if path else TABLE)
        self.keys = [z[f"k{k}"] for k in range(4)]
        self.vals = [z[f"v{k}"] for k in range(4)]
        self.cache: dict[int, float] = {}

    def logodds(self, hist, nxt: int) -> float:
        """Log-odds of a space before `nxt` after the symbols `hist` (oldest
        first; only the last three count)."""
        h = list(hist)[-3:]
        key = code(h, nxt) * 4 + len(h)
        v = self.cache.get(key)
        if v is not None:
            return v
        v = 0.0
        for k in range(len(h), -1, -1):
            c = code(h[len(h) - k:] if k else [], nxt)
            ks = self.keys[k]
            i = int(np.searchsorted(ks, c))
            if i < len(ks) and ks[i] == c:
                v = float(self.vals[k][i])
                break
        if len(self.cache) > 200000:
            self.cache.clear()
        self.cache[key] = v
        return v


_MODEL: dict[str, SpaceModel] = {}


def load(path: Path | str | None = None) -> SpaceModel:
    key = str(path or TABLE)
    if key not in _MODEL:
        _MODEL[key] = SpaceModel(path)
    return _MODEL[key]
