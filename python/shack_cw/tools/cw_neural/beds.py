#!/usr/bin/env python3
"""Cut real band noise for training: quiet 8 kHz channels from the IQ
library's recordings, inside each band's CW segment.

    python3 tools/cw_neural/beds.py OUT.npy --rec ID [ID ...] [--seconds 40] [--per 8]

Runs where the recordings are (office:/srv/iq/rec, numpy and scipy only).
Gaussian noise is not what a band sounds like: static crashes, a
neighbour's key clicks and AGC steps are what made the classic decoder
print text from 20 m noise (GUIDE §5), so half the synthetic examples
lay their signal over one of these beds instead (synth.example).

A channel is kept only if nothing keyed stands in its middle +-350 Hz:
per 3.9 Hz bin, the 95th percentile of power over 32 ms frames within
QUIET_DB of the middle's median. A CW signal 20 dB under the noise in
2.5 kHz still stands 8 dB over that in one bin, so a kept bed holds no
station - its label is empty, and a station in it would teach the
network to print nothing for one.

Never cut from the decoder bench's recordings (the Kiwi corpus, ohs-box
20m-cwskimmer-200k, the 20261012 Flex loop surveys): those are the test.
The script refuses them.

Writes OUT.npy, (beds, seconds * 8000) complex64, and OUT.json beside it
saying where each bed came from.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import fft as sfft

FS = 8000
QUIET_DB = 4.0
SEGMENTS_KHZ = [(1810, 1838), (3500, 3570), (5351, 5354), (7000, 7040), (10100, 10130), (14000, 14070),
                (18068, 18095), (21000, 21070), (24890, 24915), (28000, 28070), (50000, 50100)]
TEST_SOURCES = ("kiwisdr-", "20m-cwskimmer-200k", "20261012-flex-")
REC = Path("/srv/iq/rec")


def load(rid: str, seconds: float):
    d = REC / rid
    meta = json.loads((d / "meta.json").read_text())
    data = next(d.glob("*.sigmf-data"))
    sm = json.loads(data.with_suffix(".sigmf-meta").read_text())
    dt = sm["global"]["core:datatype"]
    rate = float(sm["global"]["core:sample_rate"])
    centre = float(meta.get("centre_hz") or sm["captures"][0]["core:frequency"])
    n = int(seconds * rate)
    if dt == "ci16_le":
        raw = np.fromfile(data, dtype="<i2", count=2 * n).astype(np.float32)
    elif dt == "cf32_le":
        raw = np.fromfile(data, dtype="<f4", count=2 * n)
    else:
        raise SystemExit(f"{rid}: {dt} not handled")
    z = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    return z, rate, centre


def quiet(c: np.ndarray) -> float:
    """dB by which the strongest bin in the middle +-350 Hz stands over the
    middle's median (95th percentiles over time)."""
    nfft, hop = 2048, 256
    fr = np.lib.stride_tricks.sliding_window_view(c, nfft)[::hop]
    if len(fr) > 1500:
        fr = fr[np.linspace(0, len(fr) - 1, 1500).astype(int)]
    P = np.abs(np.fft.fft(fr * np.hanning(nfft), axis=1)) ** 2
    f = np.fft.fftfreq(nfft, 1 / FS)
    p = np.percentile(P, 95, axis=0)
    mid = np.abs(f) <= 350
    return float(10 * np.log10(p[mid].max() / np.median(p[mid])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--rec", nargs="+", required=True)
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--per", type=int, default=8, help="beds per recording at most")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    beds, info = [], []
    for rid in a.rec:
        if any(s in rid for s in TEST_SOURCES):
            raise SystemExit(f"{rid} is one of the decoder bench's recordings: the test set, never training")
        z, rate, centre = load(rid, a.seconds)
        n, m = len(z), int(a.seconds * FS)
        Z = sfft.fft(z, workers=8)
        lo, hi = centre - 0.42 * rate, centre + 0.42 * rate
        cands = []
        for s0, s1 in SEGMENTS_KHZ:
            a0, a1 = max(lo, s0 * 1e3 + 500), min(hi, s1 * 1e3 - 500)
            if a1 > a0:
                cands += list(np.arange(a0, a1, 250.0))
        rng.shuffle(cands)
        kept: list[float] = []
        for f in cands:
            if len(kept) >= a.per:
                break
            if any(abs(f - g) < 2000 for g in kept):
                continue
            k0 = int(round((f - centre) * n / rate))
            idx = (k0 + np.fft.fftfreq(m, 1.0 / m).astype(np.int64)) % n
            c = (sfft.ifft(Z[idx], workers=8) * (m / n)).astype(np.complex64)
            q = quiet(c)
            if q < QUIET_DB:
                kept.append(f)
                beds.append(c)
                info.append({"rec": rid, "hz": f, "quiet_db": round(q, 2)})
                print(f"{rid} {f / 1e3:.2f} kHz quiet {q:.1f} dB", flush=True)
        del Z
    out = Path(a.out)
    np.save(out, np.stack(beds))
    out.with_suffix(".json").write_text(json.dumps(info, indent=1))
    print(f"{len(beds)} beds -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
