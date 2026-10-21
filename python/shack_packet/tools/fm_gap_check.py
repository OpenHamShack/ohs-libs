#!/usr/bin/env python3
"""Look for holes in a captured FM packet over: where the carrier on the
channel dropped (the DAC ran dry and only LO leakage went out, ifo_hz away),
and where the audio jumped (a discontinuity between buffers).

    fm_gap_check.py CAPTURE --rate 2000000 --centre 144937500 --freq 144937500 [--format cf32|cs16|cu8]

CAPTURE is raw IQ (cf32: float32 I,Q; cs16: int16 I,Q; cu8: RTL's) or a
SigMF .sigmf-data beside its .sigmf-meta (rate, centre and format read
from the meta). Receive-only analysis: it never touches a radio.

Per 5 ms it measures the channel's power (±8 kHz around --freq) and FM-
demodulates it. It reports:

- each **over** (channel power more than 15 dB over the capture's floor
  for 50 ms or more), with its length;
- inside an over, every **hole**: 2 ms or more with the channel power more
  than 10 dB below the over's median - what a starved DAC sends;
- inside an over, every **jump**: a demodulated sample more than JUMP_RAD
  off the median of its neighbours - what a buffer boundary with a phase
  or timing slip looks like.

A clean over prints "no holes, no jumps". Exit status 1 if any over has a
hole.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

STEP_S = 0.005
HALF_BW = 8_000.0
BRIDGE_S = 0.3
#: A discriminator sample this far (radians) off its neighbours' median is
#: a slip. A clean AFSK over stays within 0.02 rad of it at the ~64 kS/s
#: used; a 2 rad phase step between two buffers shows as about 1 rad (it
#: lands across two samples).
JUMP_RAD = 0.3


def load(path: Path, fmt: str | None, rate: float | None, centre: float | None):
    if path.suffix == ".sigmf-data":
        meta = json.loads(path.with_suffix(".sigmf-meta").read_text())
        g = meta["global"]
        rate = rate or float(g["core:sample_rate"])
        centre = centre or float(meta["captures"][0].get("core:frequency", 0.0))
        dt = g["core:datatype"]
        fmt = fmt or {"cf32_le": "cf32", "ci16_le": "cs16", "cu8": "cu8"}.get(dt, dt)
    raw = path.read_bytes()
    if fmt == "cf32":
        z = np.frombuffer(raw, np.float32).view(np.complex64)
    elif fmt == "cs16":
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        z = x.view(np.complex64)
    elif fmt == "cu8":
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 127.5) / 127.5
        z = x.view(np.complex64)
    else:
        raise SystemExit(f"unknown format {fmt!r}")
    return z, float(rate), float(centre)


def analyse(z: np.ndarray, rate: float, centre: float, freq: float) -> list[dict]:
    """The overs found, each {start_s, length_s, holes: [(t, ms)], jumps: [t]}."""
    n = len(z)
    t = np.arange(n) / rate
    bb = z * np.exp(-2j * np.pi * (freq - centre) * t).astype(np.complex64)
    # A plain low-pass: a moving average as long as the decimation (to ~4x
    # the channel), so a phase slip stays within one output sample and
    # shows as a spike; anything longer smears it into the tones.
    dec = max(1, int(rate // (8 * HALF_BW)))
    k = dec
    lp = np.convolve(bb, np.ones(k, np.complex64) / k, mode="same")[::dec]
    fs = rate / dec
    step = max(1, int(STEP_S * fs))
    m = len(lp) // step
    power = 10 * np.log10(np.mean(np.abs(lp[:m * step].reshape(m, step)) ** 2, axis=1) + 1e-20)
    audio = np.angle(lp[1:] * np.conj(lp[:-1]))
    floor = np.percentile(power, 10)
    on = power > floor + 15
    overs = []
    i = 0
    while i < m:
        if not on[i]:
            i += 1
            continue
        # An over runs on across dips of up to BRIDGE_S: a hole inside an
        # over is what is being looked for, not the over's end.
        j, bridge = i, int(BRIDGE_S / STEP_S)
        while j < m and on[j:j + bridge + 1].any():
            j += 1
        while j > i and not on[j - 1]:
            j -= 1
        if (j - i) * STEP_S >= 0.05:
            seg = power[i:j]
            med = np.median(seg)
            holes, run = [], 0
            for s in range(i + 1, j - 1):          # the edges are partial steps
                if power[s] < med - 10:
                    run += 1
                else:
                    if run * STEP_S >= 0.002:
                        holes.append((round((s - run) * STEP_S, 3), round(run * STEP_S * 1000)))
                    run = 0
            # Jumps: one-sample outliers of the discriminator against its
            # own 5-sample median (a phase slip is a spike; the tones are
            # smooth), away from the over's ragged first and last 10 ms.
            edge = int(0.01 * fs)
            lo, hi = i * step + edge, j * step - edge
            a = audio[lo:hi]
            jumps = []
            if len(a) > 10:
                med = np.median(np.stack([a[k:len(a) - 4 + k] for k in range(5)]), axis=0)
                r = np.abs(a[2:len(a) - 2] - med)
                jumps = [round((lo + 2 + int(x)) / fs, 4) for x in np.flatnonzero(r > JUMP_RAD)][:50]
            overs.append({"start_s": round(i * STEP_S, 3), "length_s": round((j - i) * STEP_S, 3),
                          "holes": holes, "jumps": jumps})
        i = j + 1
    return overs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", type=Path)
    ap.add_argument("--rate", type=float)
    ap.add_argument("--centre", type=float)
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--format", choices=["cf32", "cs16", "cu8"])
    a = ap.parse_args()
    z, rate, centre = load(a.capture, a.format, a.rate, a.centre)
    overs = analyse(z, rate, centre, a.freq)
    bad = False
    for o in overs:
        if not o["holes"] and not o["jumps"]:
            print(f"over at {o['start_s']:8.3f} s, {o['length_s']:6.2f} s: no holes, no jumps")
            continue
        bad |= bool(o["holes"])
        print(f"over at {o['start_s']:8.3f} s, {o['length_s']:6.2f} s: {len(o['holes'])} hole(s), "
              f"{len(o['jumps'])} jump(s)")
        for t, ms in o["holes"]:
            print(f"    hole at +{t - o['start_s']:.3f} s, {ms} ms")
        for t in o["jumps"][:10]:
            print(f"    jump at +{t - o['start_s']:.4f} s")
    if not overs:
        print("no overs found: is --freq right, and the capture long enough?")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
