"""Synthetic training examples: one channel as the bench's clips hear it.

Each example is EXAMPLE_S of 8 kHz complex baseband with the wanted
station near 0 Hz, the way decoder_bench/clips.py cuts a real signal,
then through the same receiver (shack_cw.benchrx.CwReceiver: NB's and
Radio's 500 Hz CW filter and medium AGC) onto a 700 Hz pitch, then
through the decoder's own front end (shack_cw.nn.front_sums). The label is
the text whose every character was keyed whole inside the example.

What real bands do to CW that the bench's clean tones did not, each drawn
at random per example (the bench's per-call recovery put most lost calls
down to weak, faded or badly keyed signals):

* SNR -16 to +30 dB in 2.5 kHz (snr_2500, as clips.py measures it),
  most of it between -12 and +12, where calls are won and lost;
* 10-45 WPM; machine, weighted keyer, human (straight key), bug (machine
  dots, human dashes), Farnsworth spacing, and a slow speed drift;
* QSB (slow sinusoidal or Rayleigh fading, down to 30 dB) and flutter;
* QRN: static crashes, short impulses and longer decaying ones;
* neighbours: other stations keying their own text 60-900 Hz away
  (inside and outside the filter), carriers;
* under a strong station's overs: its backwave (20-45 dB down) or a
  weaker station on the same frequency (10-35 dB down), not text;
* chirp at each key-down, drift, mistuning that the AFC must follow;
* hard keying (clicks) to soft edges;
* AGC pumping comes from the receiver's own AGC on all of the above;
* receivers: the bench's (most), other filter widths, and none at all (a
  skimmer channel has no filter or AGC: its filter bank is flat).
* examples with no wanted signal at all (noise, a neighbour only, a
  carrier, static), labelled empty: the network must learn to print
  nothing, which the bench's quiet targets score.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from shack_cw.benchrx import CwReceiver
from shack_cw.morse import MORSE, PROSIGNS
from shack_cw.nn import front_sums, tokens

import textgen

FS = 8000
PITCH = 700.0
EXAMPLE_S = 16.0
#: The front end whose frequencies an example's sums hold (shack_cw.nn.FRONTS):
#: the one with the most, since any front with fewer reads a subset.
SUMS_FRONT = "v2"


@dataclass
class Fist:
    wpm: float
    ratio: float = 3.0          # dash / dot
    weight: float = 0.0         # dots added to each mark (and taken from the gap after)
    jitter: float = 0.0         # sd of each element's length, as a fraction
    letter: float = 3.0         # letter gap, dots
    word: float = 7.0           # word gap, dots
    farnsworth: float = 1.0     # gaps stretched by this (characters at wpm, text slower)
    bug: bool = False           # machine dots, human dashes
    drift: float = 0.0          # speed change over the example, fraction
    period: float = 30.0        # ... as a sine of this period, s


def fist(rng: np.random.Generator) -> Fist:
    wpm = float(np.exp(rng.uniform(np.log(10), np.log(45))))
    style = rng.choice(["machine", "keyer", "human", "bug"], p=[0.3, 0.25, 0.35, 0.1])
    f = Fist(wpm)
    if style == "keyer":
        f.weight = float(rng.uniform(-0.25, 0.45))
        f.ratio = float(rng.uniform(2.8, 3.5))
        f.letter = float(rng.uniform(2.6, 3.6))
        f.word = float(rng.uniform(6.0, 8.5))
    elif style in ("human", "bug"):
        f.ratio = float(rng.uniform(2.4, 4.2))
        f.weight = float(rng.uniform(-0.2, 0.5))
        f.jitter = float(rng.uniform(0.04, 0.22))
        f.letter = float(rng.uniform(2.0, 4.5))
        f.word = float(rng.uniform(4.5, 11.0))
        f.drift = float(rng.uniform(0.0, 0.15))
        f.period = float(rng.uniform(8, 40))
        f.bug = style == "bug"
        if f.bug:
            f.ratio = float(rng.uniform(3.0, 5.0))
    if rng.random() < 0.08 and wpm > 15:
        f.farnsworth = float(rng.uniform(1.3, 2.5))
    return f


def _codes(text: str) -> list[tuple[str, str]]:
    """(symbol as printed, Morse code) per character; (" ", "") for a word space."""
    out, i, up = [], 0, text.upper()
    while i < len(up):
        ch = up[i]
        if ch == " ":
            out.append((" ", ""))
            i += 1
            continue
        if ch == "<":
            j = up.find(">", i)
            if j > 0 and up[i + 1:j] in PROSIGNS:
                out.append((up[i:j + 1], PROSIGNS[up[i + 1:j]]))
                i = j + 1
                continue
        if ch in MORSE:
            out.append((ch, MORSE[ch]))
        i += 1
    return out


def key(text: str, f: Fist, rng: np.random.Generator, t0: float):
    """Runs (key, seconds) for `text` from time t0, and each character's
    (symbol, end time). Every length is in dots of the speed at that time."""
    runs: list[tuple[int, float]] = []
    chars: list[tuple[str, float]] = []
    t = t0
    phase = rng.uniform(0, 2 * np.pi)

    def dot():
        return 1.2 / (f.wpm * (1 + f.drift * np.sin(2 * np.pi * t / f.period + phase)))

    def jit(x, on=True):
        return x * float(np.clip(rng.normal(1.0, f.jitter), 0.5, 1.6)) if on and f.jitter else x

    codes = _codes(text)
    for i, (sym, code) in enumerate(codes):
        if sym == " ":
            continue
        for k, el in enumerate(code):
            d = dot()
            if k:
                g = jit(d * (1 - f.weight) if (1 - f.weight) > 0.25 else 0.25 * d)
                runs.append((0, g))
                t += g
            mark = d * (f.ratio if el == "-" else 1.0) + f.weight * d
            mark = jit(mark, on=not (f.bug and el == "."))
            runs.append((1, max(0.25 * d, mark)))
            t += runs[-1][1]
        chars.append((sym, t))
        if i + 1 < len(codes):
            d = dot()
            if codes[i + 1][0] == " ":
                g = jit(d * f.word * f.farnsworth - f.weight * d)
                chars.append((" ", t + 0.5 * g))
            else:
                g = jit(d * f.letter * (1 + 0.5 * (f.farnsworth - 1)) - f.weight * d)
            g = max(g, 1.2 * d)
            runs.append((0, g))
            t += g
    return runs, chars, t


def envelope(runs, n: int, rate: float, t0: float, edge: float) -> np.ndarray:
    """0..1 keying at `rate` from (key, seconds) runs starting at t0, edges
    `edge` s (two passes of a moving average: a soft triangle)."""
    env = np.zeros(n)
    t = t0
    for k, d in runs:
        if k:
            a, b = int(round(t * rate)), int(round((t + d) * rate))
            env[max(0, a):max(0, min(n, b))] = 1.0
        t += d
    m = max(1, int(edge * rate))
    if m > 1:
        for _ in range(2):
            c = np.concatenate([np.zeros(m), np.cumsum(env)])
            env = (c[m:] - c[:-m]) / m
        # Centred again (each pass delayed it m/2); never wrapped round, or
        # keying at the start would reappear at the end.
        env = np.concatenate([env[m:], np.zeros(m)])
    return env


def fading(n: int, rate: float, rng, kind: str) -> np.ndarray:
    """A gain (amplitude) over time with mean power 1."""
    t = np.arange(n) / rate
    if kind == "sine":
        depth = rng.uniform(6, 30)
        per = rng.uniform(2, 15)
        db = -depth * 0.5 * (1 - np.cos(2 * np.pi * t / per + rng.uniform(0, 6.3)))
        g = 10 ** (db / 20)
    else:
        # Rayleigh (or Rician, with a steady part): complex noise low-passed
        # to a Doppler spread `fd`, as on a long skip path or auroral flutter.
        fd = rng.uniform(0.05, 0.8) if kind == "rayleigh" else rng.uniform(5, 25)
        m = max(8, int(rate / (fd * 4)))
        k = int(np.ceil(n / m)) + 4
        z = rng.standard_normal(k) + 1j * rng.standard_normal(k)
        zz = np.interp(np.arange(n) / m, np.arange(k), z.real) + 1j * np.interp(np.arange(n) / m, np.arange(k), z.imag)
        steady = rng.uniform(0, 2)
        g = np.abs(zz / np.sqrt(2) + steady)
    return g / np.sqrt(np.mean(g ** 2))


def station(rng, n: int, rate: float, text: str, f: Fist, t0: float, offset: float, *, edge: float,
            drift: float = 0.0, chirp: float = 0.0, chirp_tau: float = 0.02, qsb: str | None = None):
    """A keyed carrier at `offset` Hz: (complex samples with power 1 key-down,
    character ends, the end time of the keying)."""
    runs, chars, t_end = key(text, f, rng, t0)
    env = envelope(runs, n, rate, t0, edge)
    t = np.arange(n) / rate
    freq = offset + drift * t
    if chirp:
        on = np.flatnonzero(np.diff(np.concatenate([[0.0], (env > 0.5).astype(float)])) > 0)
        last = np.zeros(n, np.int64)
        last[on] = on
        last = np.maximum.accumulate(last)
        since = (np.arange(n) - last) / rate
        freq = freq + chirp * np.exp(-since / chirp_tau) * (last > 0)
    ph = 2 * np.pi * np.cumsum(freq) / rate + rng.uniform(0, 2 * np.pi)
    z = env * np.exp(1j * ph)
    if qsb:
        z = z * fading(n, rate, rng, qsb)
    return z, chars, t_end


def qrn(rng, n: int, rate: float, sigma: float) -> np.ndarray:
    """Static: impulses (0.3-3 ms) and crashes (5-60 ms, decaying), each
    10-40 dB over the noise in the full bandwidth."""
    z = np.zeros(n, np.complex128)
    per_s = rng.uniform(0.3, 15)
    for tt in rng.uniform(0, n / rate, rng.poisson(per_s * n / rate)):
        a0 = int(tt * rate)
        if rng.random() < 0.75:
            m = max(2, int(rng.uniform(0.0003, 0.003) * rate))
            w = np.hanning(m)
        else:
            m = max(4, int(rng.uniform(0.005, 0.06) * rate))
            w = np.exp(-np.arange(m) / (m / 4))
        lvl = sigma * 10 ** (rng.uniform(10, 40) / 20)
        b = (rng.standard_normal(m) + 1j * rng.standard_normal(m)) * w * lvl
        z[a0:a0 + m] += b[:max(0, n - a0)]
    return z


def text_for(rng, f: Fist, seconds: float) -> str:
    """Overs to fill about `seconds` at this fist's speed, with pauses
    between overs written as runs of spaces (one space is a word gap)."""
    me = textgen.callsign(rng)
    words: list[str] = []
    budget = seconds * f.wpm / 1.2 / 50 * 1.3          # words a second at PARIS, and some
    while sum(len(w) + 1 for w in words) / 5 < budget:
        ov = textgen.over(rng, me=me if rng.random() < 0.7 else None)
        if rng.random() < 0.1 and " " in ov:          # <BT> between thoughts, which r1 printed as X
            i = int(rng.choice([k for k, ch in enumerate(ov) if ch == " "]))
            ov = ov[:i] + " <BT>" + ov[i:]
        words.append(ov)
    return " | ".join(words)


@dataclass
class Example:
    feat: np.ndarray          # the front end's sums (frames, len(offsets(SUMS_FRONT))) complex; or audio
    label: list[int]          # SYMBOLS indices
    text: str
    wpm: float
    t_first: float            # the wanted station's first and last key-down, s (0 if none)
    t_last: float
    snr: float
    kind: str


def example(rng: np.random.Generator, seconds: float = EXAMPLE_S, noise_bed=None, want_audio: bool = False,
            profile: str = "all") -> Example:
    """One example: its `feat` is the decoder front end's complex sums at
    every frequency SUMS_FRONT reads (a superset of v1's), from which
    training makes any front's features; with `want_audio` it is the
    receiver's audio (8 kHz) instead, for a decoder to be fed as an app
    would (val.py).

    `profile` "weak" draws seven signals in ten from -14 to +5 dB and makes
    one in six an NCDXF-style over (a call, then four 1 s dashes): the
    network read weak beacons worst of everything on real recordings
    (29 % CER at -5..5 dB on realval's 47 slots, 6 % at 5..15). "cochan"
    is "all" with a weaker station on the wanted one's frequency in one
    signal example in two."""
    n = int(seconds * FS)
    sigma = 1.0                                   # complex noise sd per component
    n0 = 2 * sigma ** 2 / FS                      # W/Hz
    u = rng.random()
    kind = "signal" if u < 0.86 else "noise" if u < 0.91 else "neighbour" if u < 0.97 else "carrier"
    s = rng.random()
    # Strong signals to 60 dB: a local station is 40-50 dB over the noise in
    # 2.5 kHz, and a network trained to 30 dB printed nothing at 45 (its
    # noise references then hold the tone's own main lobe and keying).
    snr = (rng.uniform(-12, 12) if s < 0.55 else rng.uniform(12, 30) if s < 0.77 else rng.uniform(30, 60)
           if s < 0.89 else rng.uniform(-16, -12))
    if profile == "weak":
        snr = rng.uniform(-14, 5) if s < 0.7 else rng.uniform(5, 15) if s < 0.9 else rng.uniform(15, 40)
    z = np.zeros(n, np.complex128)
    label_text, wpm, t_first, t_last = "", 0.0, 0.0, 0.0
    f = fist(rng)
    edge = float(np.exp(rng.uniform(np.log(0.0008), np.log(0.008))))
    if kind == "signal":
        lead = float(rng.uniform(0.0, 3.0)) if rng.random() < 0.8 else 0.0
        stop = seconds - float(rng.uniform(0.4, 2.0))
        raw = text_for(rng, f, stop - lead)
        # Pauses between overs ("|"): 1.5-6 s of silence, and the text is
        # keyed over by over so each character's time is known.
        chars: list[tuple[str, float]] = []
        runs_all = []
        t = lead
        for k, ov in enumerate(raw.split(" | ")):
            pause = []
            if k:
                gap = float(rng.uniform(1.0, 6.0))
                pause = [(" ", t + gap / 2)]
                t += gap
            r, ch, t_end = key(ov, f, rng, t)
            runs_all.append((t, r, pause + ch))
            t = t_end
        # Keep the characters that end (with their gap seen) before `stop`;
        # nothing after them is keyed, so no letter is cut in half.
        keep_runs = []
        for t0, r, ch in runs_all:
            ok = [c for c in ch if c[1] <= stop]
            if not ok:
                break
            tt, rr = t0, []
            for kk, d in r:
                if tt + d > ok[-1][1] + 1e-9 and kk:
                    break
                rr.append((kk, d))
                tt += d
            keep_runs.append((t0, rr))
            chars += ok
            if len(ok) < len(ch):
                break
        while chars and chars[-1][0] == " ":
            chars.pop()
        env = np.zeros(n)
        for t0, rr in keep_runs:
            env = np.maximum(env, envelope(rr, n, FS, t0, edge))
        tt = np.arange(n) / FS
        off = float(rng.normal(0, 6)) if rng.random() < 0.85 else float(rng.uniform(-25, 25))
        drift = float(rng.uniform(-1.5, 1.5)) if rng.random() < 0.2 else 0.0
        freq = off + drift * (tt - seconds / 2)
        if rng.random() < 0.1:
            on = np.flatnonzero(np.diff(np.concatenate([[0.0], (env > 0.5).astype(float)])) > 0)
            last = np.zeros(n, np.int64)
            last[on] = on
            last = np.maximum.accumulate(last)
            freq = freq + rng.uniform(10, 80) * rng.choice([-1, 1]) * np.exp(
                -(np.arange(n) - last) / FS / rng.uniform(0.005, 0.05)) * (last > 0)
        ph = np.exp(1j * (2 * np.pi * np.cumsum(freq) / FS + rng.uniform(0, 2 * np.pi)))
        # Round 2 (14/10/2026): what sits under a strong station's own
        # keying. On a 38 dB QSO in realval's dev set (DF2IC) the network
        # printed nothing usable where the classic engine read it: the
        # tone in its gaps stood 5-10 dB over the noise beside it - the
        # transmitter's backwave, or the other station of the QSO on the
        # same frequency - and a network that has only seen clean gaps
        # reads anything 9 dB up as a mark. Both only while the wanted
        # station transmits, so a weak station alone in a pause (the next
        # NCDXF beacon) is still a signal to read.
        over = np.zeros(n)
        for t0, rr in keep_runs:
            over[int(t0 * FS):int((t0 + sum(d for _, d in rr)) * FS)] = 1.0
        if snr > 12 and rng.random() < 0.2:
            env = env + 10 ** (-rng.uniform(20, 45) / 20) * over * (1 - env)
        sig = env * ph
        # Profile "cochan" makes it one in two and from 6 dB down: with a
        # station 15 dB under a 38 dB one, 8 Hz away and keying at once,
        # every network so far printed rubbish where the classic engine
        # read every letter (tools: a probe in the round-2 notes).
        if profile == "cochan" and snr > -2 and rng.random() < 0.5:
            rel = rng.uniform(6, 30)
        elif snr > 10 and rng.random() < 0.15:
            rel = rng.uniform(10, 35)
        else:
            rel = None
        if rel is not None:
            g = fist(rng)
            w, _, _ = station(rng, n, FS, text_for(rng, g, seconds), g, float(rng.uniform(-3, 3)),
                              off + float(rng.normal(0, 15)), edge=float(np.exp(rng.uniform(np.log(0.001), np.log(0.008)))))
            z += np.sqrt(10 ** ((snr - rel) / 10) * n0 * 2500) * w * over
        q = rng.random()
        if q < 0.25:
            sig = sig * fading(n, FS, rng, "sine")
        elif q < 0.45:
            sig = sig * fading(n, FS, rng, "rayleigh")
        elif q < 0.48:
            sig = sig * fading(n, FS, rng, "flutter")
        z += np.sqrt(10 ** (snr / 10) * n0 * 2500) * sig
        label_text = "".join(c for c, _ in chars)
        wpm = f.wpm
        on = np.flatnonzero(env > 0.5)
        if len(on):
            t_first, t_last = on[0] / FS, on[-1] / FS
        # NCDXF-style: the call, then four 1 s dashes that are not text.
        if rng.random() < (0.17 if profile == "weak" else 0.04) and keep_runs:
            t0 = (on[-1] / FS if len(on) else 0) + 0.3
            dash = np.zeros(n)
            for i in range(4):
                a, b = int((t0 + 1.2 * i) * FS), int((t0 + 1.2 * i + 1.0) * FS)
                dash[a:min(n, b)] = 10 ** (-3 * i / 20)
            z += np.sqrt(10 ** (snr / 10) * n0 * 2500) * dash * np.exp(2j * np.pi * off * tt)
    # Neighbours: other stations keying their own text.
    nb = 0 if kind in ("noise", "carrier") else (1 if kind == "neighbour" else int(rng.choice([0, 1, 2, 3], p=[0.45, 0.33, 0.15, 0.07])))
    # A pileup: several callers 70-250 Hz either side, about as strong as
    # the wanted station, as on a busy band or round a DX station.
    pile = kind == "signal" and rng.random() < 0.12
    if pile:
        nb = int(rng.integers(2, 5))
    for _ in range(nb):
        g = fist(rng)
        o = float(rng.uniform(60, 900)) * rng.choice([-1, 1])
        if kind == "neighbour":
            o = float(rng.uniform(110, 900)) * rng.choice([-1, 1])
        lvl = snr + float(rng.uniform(-10, 30)) if kind == "signal" else float(rng.uniform(-5, 40))
        if pile:
            o = float(rng.uniform(70, 250)) * rng.choice([-1, 1])
            lvl = snr + float(rng.uniform(-6, 8))
        w, _, _ = station(rng, n, FS, text_for(rng, g, seconds), g, float(rng.uniform(-3, 3)), o,
                          edge=float(np.exp(rng.uniform(np.log(0.001), np.log(0.008)))),
                          qsb="rayleigh" if rng.random() < 0.3 else None)
        z += np.sqrt(10 ** (min(lvl, 65.0) / 10) * n0 * 2500) * w
    if kind == "carrier" or rng.random() < 0.05:
        o = float(rng.uniform(40, 400)) * rng.choice([-1, 1]) if kind != "carrier" or rng.random() < 0.5 else float(rng.normal(0, 10))
        z += np.sqrt(10 ** (rng.uniform(-5, 30) / 10) * n0 * 2500) * np.exp(2j * np.pi * (o * np.arange(n) / FS + rng.uniform(0, 1)))
    # The noise: Gaussian, or a real band's (noise_bed: complex 8 kHz, any
    # level), scaled to the same density in the passband.
    if noise_bed is not None and rng.random() < 0.5:
        a = int(rng.integers(0, max(1, len(noise_bed) - n)))
        bed = noise_bed[a:a + n]
        if len(bed) < n:
            bed = np.resize(bed, n)
        z += bed / np.sqrt(np.mean(np.abs(bed) ** 2) / (2 * sigma ** 2))
    else:
        z += sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    if rng.random() < 0.3:
        z += qrn(rng, n, FS, sigma)
    # The receiver.
    r = rng.random()
    x = z * np.exp(2j * np.pi * PITCH * np.arange(n) / FS)
    if r < 0.75:
        rx = CwReceiver(FS, PITCH, 500.0)
    elif r < 0.87:
        rx = CwReceiver(FS, PITCH, float(rng.choice([250.0, 300.0, 400.0, 600.0, 800.0, 1200.0, 2400.0])))
    else:
        rx = None
    if rx is not None:
        blk = int(0.075 * FS)
        audio = np.concatenate([rx(x[i:i + blk]) for i in range(0, n, blk)])
    else:
        audio = (x.real * 10 ** rng.uniform(-3, 1)).astype(np.float32)
    afc = rng.random() < 0.8
    feat = audio.astype(np.float32) if want_audio else front_sums(audio.astype(np.float32), FS, PITCH, afc=afc,
                                                                  front=SUMS_FRONT)
    return Example(feat, tokens(label_text), label_text, wpm, t_first, t_last, snr, kind)
