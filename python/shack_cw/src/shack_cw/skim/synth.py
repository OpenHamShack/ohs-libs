"""Synthetic CW bands for the skimmer: a complex segment carrying many
stations at once, with the ground truth to score against.

Each station keys its own messages (a CQ, a contest CQ, a QSO's "DE"
over) at its own speed, human or machine timing (shack_cw.bench's
runs_for), with QSB and drift if asked, from its own start time; between
overs it is silent for a few seconds, as a running station is. Steady
carriers, a DC spur and static crashes can be added. SNR is the RBN's: a
station's key-down power over the noise in 500 Hz.

Used by the skimmer's tests, the bench (shack_cw.skim.bench), and the
apps' self-tests (a frozen desktop build decodes `selftest_band()`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..bench import runs_for

# Calls with real prefixes in many entities, for random bands. None is a
# real station's call as far as anyone knows here; the shapes are what
# matters.
CALLS = ["DL1ABC", "G4XYZ", "F5NQL", "EA3XX", "I2ABC", "OK1AB", "SP9XYZ", "UA3ABC", "JA1ABC", "K1ABC",
         "W2XYZ", "VE3ABC", "PA3ABC", "ON4ABC", "OH2XX", "SM5ABC", "LA9XX", "OZ1ABC", "HB9ABC", "OE3ABC",
         "YU1AB", "LZ2ABC", "YO3ABC", "HA5AB", "S51AB", "9A2AB", "EI4AB", "GM3ABC", "GW4ABC", "CT1ABC",
         "UR5ABC", "LY2AB", "YL2AB", "ES5AB", "OM3AB", "DJ2XX", "DK5AB", "IK2XYZ", "EA5AB", "F6ABC",
         "PY2AB", "LU1AB", "ZL2AB", "VK3AB", "N4XY", "KA1ABC", "W9XY", "4X1AB", "SV1AB", "TA1AB"]


@dataclass
class Station:
    call: str
    offset: float                    # Hz from the segment's centre
    wpm: float = 24.0
    snr: float = 15.0                # dB in 500 Hz, key-down
    style: str = "cq"                # cq, test, de (answers another call), beacon
    human: bool = False
    qsb: bool = False
    drift: float = 0.0               # Hz per second
    start: float = 0.5               # seconds into the band
    gap: float = 3.0                 # seconds between overs
    other: str = "GM4BRI"            # whom a "de" station is working
    messages: list = field(default_factory=list)   # what was sent, for scoring

    def text(self) -> str:
        c = self.call
        if self.style == "test":
            return f"CQ TEST {c} {c}"
        if self.style == "de":
            return f"{self.other} DE {c} {c} TU 5NN K"
        if self.style == "beacon":
            return f"VVV VVV DE {c} {c}"
        return f"CQ CQ DE {c} {c} K"


def _envelope(runs, rate: float, edge: float = 0.005) -> np.ndarray:
    """The keying as 0/1 with soft edges: two passes of a moving average
    `edge` long (a triangle, near enough bench.key_envelope's raised
    cosine). bench's np.convolve is fine for one signal at 12 kHz and took
    a minute for thirty at 96 kHz; running sums take a second."""
    lengths = np.array([max(1, int(round(d * rate))) for _, d in runs])
    env = np.repeat(np.array([s for s, _ in runs], np.float64), lengths)
    m = max(1, int(edge * rate))
    for _ in range(2):
        c = np.concatenate([np.zeros(m), np.cumsum(env)])
        env = (c[m:] - c[:-m]) / m
    return np.roll(env, -m)


def render(stations: list[Station], seconds: float, rate: float = 96_000.0, *, seed: int = 0,
           carriers: list[tuple[float, float]] = (), dc_db: float | None = None, static: float = 0.0,
           noise: bool = True) -> np.ndarray:
    """The segment as complex64 at `rate`, `seconds` long. `carriers`:
    (offset Hz, SNR dB in 500 Hz) of steady unkeyed lines; `dc_db`: a spur
    at 0 Hz; `static`: crashes a second (each 20 dB over the noise in the
    full bandwidth)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    z = np.zeros(n, np.complex128)
    t = np.arange(n) / rate
    n0 = 1.0 / 500.0                       # noise W/Hz: 0 dB in 500 Hz is power 1
    for st in stations:
        runs: list = [(0, st.start)]
        sent = []
        while sum(d for _, d in runs) < seconds:
            msg = st.text()
            sent.append(msg)
            runs += runs_for(msg, st.wpm, rng if st.human else None)
            runs.append((0, st.gap * (rng.uniform(0.7, 1.3) if st.human else 1.0)))
        st.messages = sent
        env = _envelope(runs, rate)[:n]
        env = np.concatenate([env, np.zeros(n - len(env))])
        f = st.offset + st.drift * t
        sig = env * np.exp(2j * np.pi * (np.cumsum(f) / rate + rng.uniform(0, 1)))
        a = np.sqrt(10 ** (st.snr / 10) * n0 * 500.0)
        if st.qsb:
            period = rng.uniform(6, 10)
            sig *= 10 ** (-12 * 0.5 * (1 - np.cos(2 * np.pi * t / period + rng.uniform(0, 6.3))) / 20)
        z += a * sig
    for off, snr in carriers:
        z += np.sqrt(10 ** (snr / 10)) * np.exp(2j * np.pi * (off * t + rng.uniform(0, 1)))
    if dc_db is not None:
        z += np.sqrt(10 ** (dc_db / 10))
    sigma = np.sqrt(n0 * rate / 2)
    if noise:
        z += sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    if static:
        for tt in rng.uniform(0, seconds, rng.poisson(static * seconds)):
            m = max(2, int(rng.uniform(0.0005, 0.003) * rate))
            a0 = int(tt * rate)
            burst = rng.standard_normal(m) + 1j * rng.standard_normal(m)
            z[a0:a0 + m] += (sigma * 10 ** (20 / 20) * np.hanning(m) * burst)[:max(0, n - a0)]
    return z.astype(np.complex64)


def random_band(count: int, span: float, *, seed: int = 0, min_gap: float = 150.0, snr=(8.0, 30.0),
                wpm=(14.0, 36.0), calls: list[str] | None = None) -> list[Station]:
    """`count` stations spread over +-span/2 Hz, at least `min_gap` apart,
    a third of them human, a quarter with QSB, some drifting."""
    rng = np.random.default_rng(seed + 1000)
    pool = list(calls or CALLS)
    rng.shuffle(pool)
    offs: list[float] = []
    tries = 0
    while len(offs) < count and tries < 100000:
        tries += 1
        f = float(rng.uniform(-span / 2, span / 2))
        if all(abs(f - g) >= min_gap for g in offs):
            offs.append(f)
    out = []
    styles = ["cq", "cq", "test", "de"]
    for i, f in enumerate(offs):
        out.append(Station(call=pool[i % len(pool)], offset=f, wpm=float(rng.uniform(*wpm)),
                           snr=float(rng.uniform(*snr)), style=styles[i % len(styles)], human=i % 3 == 0,
                           qsb=i % 4 == 1, drift=float(rng.uniform(-0.5, 0.5)) if i % 5 == 2 else 0.0,
                           start=float(rng.uniform(0.2, 4.0)), gap=float(rng.uniform(2.0, 5.0))))
    return out


def selftest_band(rate: float = 24_000.0, seconds: float = 40.0) -> tuple[np.ndarray, list[Station]]:
    """A small fixed band any build can decode in a few seconds: four
    stations, a carrier, noise. What a frozen desktop app's
    --cw-skim-selftest feeds its skimmer."""
    st = [Station("DL1ABC", -4000.0, 22.0, 20.0, "cq"), Station("G4XYZ", -1500.0, 28.0, 18.0, "test"),
          Station("OK1AB", 1200.0, 18.0, 22.0, "de"), Station("F5NQL", 3800.0, 25.0, 16.0, "cq", human=True)]
    return render(st, seconds, rate, seed=7, carriers=[(2500.0, 20.0)]), st
