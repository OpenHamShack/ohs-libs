"""Per-client decimation and quantisation (CLIENT-UI-PLAN.md §2.1, §2.3).

The server reduces each client's view to at most the columns it draws, so
the engine's FFT is never changed for a client (the Flex display_size and
NB wideBins traps): receive SNR is untouched whoever is watching.

Every boundary here is integer arithmetic on bin indices, so a Kotlin,
Swift or JS feed adapter (§2.5) given the same bins picks exactly the same
bins for each column; float boundaries would differ in the last place
between languages and move a bin between columns.
"""

from __future__ import annotations

import math

import numpy as np


def crop(src_lo_hz: float, bin_hz: float, n_bins: int, lo_hz: float | None, hi_hz: float | None) -> tuple[int, int]:
    """The source bins [i0, i1) that cover lo_hz..hi_hz, widened to whole
    bins and clipped to the stream's span: zooming beyond the span is a
    tune, an explicit Control action, never implied by a subscription."""
    i0 = 0 if lo_hz is None else math.floor((lo_hz - src_lo_hz) / bin_hz)
    i1 = n_bins if hi_hz is None else math.ceil((hi_hz - src_lo_hz) / bin_hz)
    i0 = min(max(i0, 0), n_bins - 1)
    i1 = min(max(i1, i0 + 1), n_bins)
    return i0, i1


def reduce(src_db, src_lo_hz: float, bin_hz: float, cols: int, lo_hz: float | None = None,
           hi_hz: float | None = None, mode: str = "peak") -> tuple[float, float, np.ndarray]:
    """One client's columns from a source row of dB bins.

    Returns (loHz of column 0's left edge, colHz, dB per column). Never
    more columns than bins: drawing one bin as two columns would invent
    resolution, and the client stretches anyway.

    peak takes a column's highest bin, so a narrow carrier never vanishes
    when zoomed out (QO-100 NB today). mean is the power average, not the
    dB average: averaging dB under-reads noise by ~2.5 dB and makes the
    floor look smoother than the engine sees it.
    """
    if mode not in ("peak", "mean"):
        raise ValueError(f"reduce must be peak or mean, not {mode!r}")
    if cols < 1:
        raise ValueError("cols must be at least 1")
    src = np.asarray(src_db, dtype=np.float64)
    i0, i1 = crop(src_lo_hz, bin_hz, len(src), lo_hz, hi_hz)
    m = i1 - i0
    n = min(cols, m)
    # Column k takes bins [k*m//n, (k+1)*m//n) of the crop: every column at
    # least one bin, widths differing by at most one.
    edges = (np.arange(n + 1) * m) // n
    win = src[i0:i1]
    if mode == "peak":
        out = np.maximum.reduceat(win, edges[:-1])
    else:
        p = np.power(10.0, win / 10.0)
        out = 10.0 * np.log10(np.add.reduceat(p, edges[:-1]) / np.diff(edges))
    return src_lo_hz + i0 * bin_hz, m * bin_hz / n, out


def quantise(db, db_floor: float, db_step: float, depth: int = 8) -> np.ndarray:
    """dB to row values: v = (dB - floor) / step, clamped to 0 and the top.

    Rounds half up (floor(x + 0.5)) rather than numpy's half-to-even, so
    every language's decoder vectors agree on the .5 cases."""
    top = (1 << depth) - 1
    v = np.floor((np.asarray(db, dtype=np.float64) - db_floor) / db_step + 0.5)
    return np.clip(v, 0, top).astype(np.uint16 if depth == 16 else np.uint8)


def dequantise(values, db_floor: float, db_step: float) -> np.ndarray:
    return db_floor + np.asarray(values, dtype=np.float64) * db_step
