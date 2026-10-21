"""Character error rate of the CW decoder under the conditions it meets on
air, and its CPU cost.

    python3 -m shack_cw.bench [--seeds 3] [--snr 0,3,6,9,12,15] [--only beacon,...]
    python3 -m shack_cw.bench --rate 24000          # the same cases at Flex's rate
    python3 -m shack_cw.bench --recording BEACON.npy   # complex 12 kHz, space tone at 0 Hz

Moved from QO-100 NB (openhamshack-nb tools/cw_bench.py, main 47be138) with the
decoder; at the default rate it prints NB's table exactly. Importable, so
an app's tests hold the shared decoder to the same cases through its own
receiver (`decode(..., receiver=)`, `run_case(..., receiver=)`).

Every case is rendered as complex baseband with the dial at 0 Hz, put
through a receiver's CW demodulator (shack_cw.benchrx.CwReceiver, NB's: the
500 Hz CW filter round the pitch and the medium AGC) in NB's engine's 75 ms
blocks (900 samples at 12 kHz), and the audio handed to the decoder block
by block, as NB's Engine._decode_loop does. SNR is the carrier's power over
the noise in 2.5 kHz, the way the transponder's signals are usually quoted.

The beacon case is QO-100's lower beacon as it is on air (DK0SB, measured
from Brian's recording of 07/10/2026; NB's ohscore/devices/simsignals.py
simulates the same): F1A, 400 Hz shift, the mark the upper tone,
12 WPM, "HI DE QO100 QRA DK0SB" then 12 s of key-down. `beacon-bookmark`
is the same signal received at the old Lower beacon bookmark (dial 700 Hz
under the space tone), which put the *space* tone on the pitch: the keying
the decoder saw was inverted - the failure on Brian's TV of 10/10/2026.

Impairments: `qsb` fades the signal 12 dB and back with a 6-10 s period;
`drift` walks the tone +-25 Hz over the run; `human` keys with a sender's
timing (each element and gap off by ~12 %, a personal dah ratio of
2.6-3.6, letter gaps of 2.2-4 dits and word gaps of 5-9); `machine` is
exact. `noise` is the receiver's noise and nothing else, and `carriers`
the same with three unkeyed carriers in the filter (60 Hz below the pitch
- inside AFC's reach - and 150 and 220 Hz above), 6 dB over the SNR: for
those two the figure is characters printed per minute, which should be 0.
`beacon-beside` is the beacon 60 Hz above the pitch with an unkeyed carrier
of the same strength 150 Hz below it.

From the corpus of real bands (shack_cw.corpus, 13/10/2026): `heavy` and
`light` weight a human's keying by +0.35 and -0.3 of a dot (marks longer,
gaps as much shorter, and the reverse); `cq25-flutter` is 15 dB fades
every 1-3 s; `cq20-two` has a second station 80 Hz above, 6 dB down, at
26 WPM, and is scored against the stronger. Three more score characters a
minute with nothing on the pitch: `qrn` is static (broadband bursts of
0.5-3 ms, eight a second, 20 dB over the SNR), `neighbour-200` a keyed
station 200 Hz away and `neighbour-wide` one 600 Hz away inside a 1500 Hz
filter - Brian's 20 m report - each 15 dB over the SNR. CER is the edit distance between the decoded text and what was sent,
over the length of what was sent, with runs of spaces counted as one.

A developer's measuring tool: its output is English for whoever runs it,
like a log line, and is not translated.
"""
from __future__ import annotations

import argparse
import time
from typing import Callable

import numpy as np

from .benchrx import CwReceiver
from .decoder import Decoder
from .morse import text_to_codes

FS = 12_000
BLOCK_S = 0.075      # what NB's Engine hands the decoder per receive block
BLOCK = 900          # BLOCK_S at FS
PITCH = 700.0
CQ_TEXT = "CQ CQ DE GM4BRI GM4BRI IO75RL K"

# QO-100's lower beacon (DK0SB), as measured from Brian's recording of
# 07/10/2026; NB's simulator (ohscore/devices/simsignals.py) uses the same.
BEACON_TEXT = "HI DE QO100 QRA DK0SB"
BEACON_WPM = 12.0
BEACON_CARRIER_S = 12.0
BEACON_GAP_S = 1.3

# A receiver: made for a sample rate and a pitch, then called with each
# block of complex baseband, answering the block's audio.
Receiver = Callable[[float, float], Callable[[np.ndarray], np.ndarray]]


def runs_for(text: str, wpm: float, rng: np.random.Generator | None = None) -> list[tuple[int, float]]:
    """(key, seconds) for `text`; with `rng`, keyed by a human."""
    dit = 1.2 / wpm
    codes = text_to_codes(text)
    ratio = rng.uniform(2.6, 3.6) if rng is not None else 3.0

    def j(x):
        return x * float(np.clip(rng.normal(1.0, 0.12), 0.6, 1.5)) if rng is not None else x

    out: list[tuple[int, float]] = []
    for i, code in enumerate(codes):
        if code == " ":
            continue
        for k, sym in enumerate(code):
            if k:
                out.append((0, j(dit)))
            out.append((1, j(dit * (ratio if sym == "-" else 1.0))))
        if i + 1 < len(codes):
            if codes[i + 1] == " ":
                out.append((0, dit * (rng.uniform(5, 9) if rng is not None else 7.0)))
            else:
                out.append((0, dit * (rng.uniform(2.2, 4.0) if rng is not None else 3.0)))
    return out


def weighted(runs: list[tuple[int, float]], wpm: float, by: float) -> list[tuple[int, float]]:
    """A keyer's weighting: every mark `by` dots longer (shorter if
    negative) and the gap after it as much shorter, so the rhythm stays
    and only the mark/space balance moves - heavy bug keying, or a
    receiver's AGC and filter stretching each element."""
    d = by * 1.2 / wpm
    out, carry = [], 0.0
    for key, dur in runs:
        dur += d if key else -carry
        carry = d if key else 0.0
        out.append((key, max(0.2 * 1.2 / wpm, dur)))
    return out


def impulses(n: int, rng, snr_db: float, rate=FS, per_s: float = 8.0) -> np.ndarray:
    """Static: bursts of 0.5-3 ms at random, `per_s` a second, each
    `snr_db` + 20 dB over the noise render() adds at 0 dB, measured in the
    full bandwidth. Broadband, so each lands on the tone and beside it at
    once, as a lightning crash or a neighbour's key click does."""
    z = np.zeros(n, np.complex128)
    sigma = np.sqrt(1.0 / 2500.0 * rate / 2)            # render()'s noise at 0 dB
    for t in rng.uniform(0, n / rate, rng.poisson(per_s * n / rate)):
        m = max(2, int(rng.uniform(0.0005, 0.003) * rate))
        a = int(t * rate)
        burst = rng.standard_normal(m) + 1j * rng.standard_normal(m)
        z[a:a + m] += (sigma * 10 ** ((snr_db + 20) / 20) * np.hanning(m) * burst)[:max(0, n - a)]
    return z.astype(np.complex64)


def beacon_runs(cycles: int) -> list[tuple[int, float]]:
    one = runs_for(BEACON_TEXT, BEACON_WPM) + [(0, 7 * 1.2 / BEACON_WPM), (1, BEACON_CARRIER_S), (0, BEACON_GAP_S)]
    return one * cycles


def key_envelope(runs, edge=0.005, rate=FS) -> np.ndarray:
    states = np.array([s for s, _ in runs], np.float64)
    lengths = np.array([max(1, int(round(d * rate))) for _, d in runs])
    env = np.repeat(states, lengths)
    n = max(1, int(edge * rate))
    w = np.hanning(2 * n)
    w /= w.sum()
    return np.convolve(env, w, mode="same")


def render(runs, rng, snr_db, *, f1a=False, mark_hz=PITCH, space_hz=None, qsb=False, drift=False,
           lead=1.0, tail=1.5, rate=FS) -> np.ndarray:
    pad0, pad1 = int(lead * rate), int(tail * rate)
    env = np.concatenate([np.zeros(pad0), key_envelope(runs, rate=rate), np.zeros(pad1)])
    n = len(env)
    t = np.arange(n) / rate
    df = np.zeros(n)
    if drift:
        # A slow random walk, smoothed, scaled to peak at +-25 Hz; one step
        # every 0.1 s whatever the rate (1200 samples at 12 kHz).
        step = int(round(0.1 * rate))
        w = np.cumsum(rng.normal(0, 1, n // step + 2))
        w = np.interp(np.arange(n), np.arange(len(w)) * step, w)
        w -= w.mean()
        df = 25.0 * w / (np.max(np.abs(w)) + 1e-9)
    if f1a:
        # One oscillator pulled between the tones (the beacon's FSK), so
        # the carrier never stops: key-up is the space tone.
        f = space_hz + (mark_hz - space_hz) * env + df
        z = np.exp(2j * np.pi * np.cumsum(f) / rate)
    else:
        z = env * np.exp(2j * np.pi * np.cumsum(mark_hz + df) / rate)
    if qsb:
        # "fast": the flutter of a high-latitude path (OH2B on 20 m in the
        # corpus lost two dots of one letter in it), 15 dB every 1-3 s.
        period, depth = (rng.uniform(1, 3), 15.0) if qsb == "fast" else (rng.uniform(6, 10), 12.0)
        fade_db = -depth * 0.5 * (1 - np.cos(2 * np.pi * t / period + rng.uniform(0, 2 * np.pi)))
        z = z * 10 ** (fade_db / 20)
    n0 = 1.0 / (10 ** (snr_db / 10) * 2500.0)          # carrier power 1: noise W/Hz
    sigma = np.sqrt(n0 * rate / 2)
    z = z + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return z.astype(np.complex64)


def demodulate(z: np.ndarray, pitch=PITCH, *, rate=FS, receiver: Receiver | None = None):
    rx = (receiver or CwReceiver)(rate, pitch)
    block = int(round(BLOCK_S * rate))
    for i in range(0, len(z), block):
        yield rx(z[i:i + block])


def decode(z: np.ndarray, pitch=PITCH, decoder=None, *, rate=FS,
           receiver: Receiver | None = None) -> tuple[str, float, float]:
    """(text, decoder CPU seconds, audio seconds)."""
    blocks = list(demodulate(z, pitch, rate=rate, receiver=receiver))
    ev: list = []
    dec = (decoder or Decoder)(ev.append, freq=pitch)
    c0 = time.process_time()
    for a in blocks:
        dec.feed(a, rate)
    cpu = time.process_time() - c0
    return "".join(e["text"] for e in ev), cpu, len(z) / rate


def _norm(s: str) -> str:
    return " ".join(s.upper().split())


def lev(a: str, b: str) -> int:
    prev = np.arange(len(b) + 1)
    for i, ca in enumerate(a, 1):
        cur = np.empty_like(prev)
        cur[0] = i
        eq = np.array([ca == cb for cb in b])
        # Row update: substitution/match from prev, deletion from prev, insertion from cur.
        sub = prev[:-1] + ~eq
        dele = prev[1:] + 1
        best = np.minimum(sub, dele)
        for k in range(1, len(b) + 1):
            cur[k] = min(best[k - 1], cur[k - 1] + 1)
        prev = cur
    return int(prev[-1])


def cer(decoded: str, sent: str) -> float:
    d, s = _norm(decoded), _norm(sent)
    return lev(d, s) / max(1, len(s))


CASES = {
    # name: (text, wpm, f1a, human, qsb, drift)
    "beacon": ("beacon", BEACON_WPM, True, False, False, False),
    "beacon-qsb": ("beacon", BEACON_WPM, True, False, True, False),
    "beacon-drift": ("beacon", BEACON_WPM, True, False, False, True),
    "beacon-bookmark": ("beacon", BEACON_WPM, "bookmark", False, False, False),
    "cq20-machine": (CQ_TEXT, 20, False, False, False, False),
    "cq20-human": (CQ_TEXT, 20, False, True, False, False),
    "cq20-human-qsb-drift": (CQ_TEXT, 20, False, True, True, True),
    "cq12-human": (CQ_TEXT, 12, False, True, False, False),
    "cq30-machine": (CQ_TEXT, 30, False, False, False, False),
    "beacon-beside": ("beacon", BEACON_WPM, "beside", False, False, False),
    "noise": ("", 0, False, False, False, False),
    "carriers": ("", 0, "carriers", False, False, False),
    # Added 13/10/2026 with the corpus of real bands (shack_cw.corpus), for
    # what it showed the cases above did not have.
    "cq20-heavy": (CQ_TEXT, 20, "heavy", True, False, False),
    "cq20-light": (CQ_TEXT, 20, "light", True, False, False),
    "cq25-flutter": (CQ_TEXT, 25, False, True, "fast", False),
    "cq20-two": (CQ_TEXT, 20, "two", True, False, False),
    "qrn": ("", 0, "qrn", False, False, False),
    "neighbour-200": ("", 0, "keyed-200", False, False, False),
    "neighbour-wide": ("", 0, "keyed-wide", False, False, False),
}
SECOND_TEXT = "TEST DE OH2XYZ OH2XYZ TEST"   # cq20-two's other station
NOISE_S = 60.0       # how long the noise cases run


def carrier(n: int, hz: float, snr_db: float, rate=FS) -> np.ndarray:
    """An unkeyed carrier `snr_db` over the noise render() adds at 0 dB."""
    return (10 ** (snr_db / 20) * np.exp(2j * np.pi * hz * np.arange(n) / rate)).astype(np.complex64)


def run_case(name: str, snr: float, seed: int, decoder=None, *, rate=FS, receiver: Receiver | None = None):
    """(score, decoder CPU seconds, audio seconds, decoded text). The score
    is the CER, or for the noise cases characters printed per minute."""
    text, wpm, f1a, human, qsb, drift = CASES[name]
    rng = np.random.default_rng(seed * 1000 + int(snr * 10))
    kw = {"rate": rate}
    if not text:
        # Nothing keyed on the pitch: the SNR sets only the strength of what
        # is beside it - the carriers 6 dB over it; the static's crashes and
        # the keyed neighbours 20 dB and 15 dB over it. neighbour-wide is
        # Brian's 20 m report: the station 600 Hz from the pitch inside a
        # 1500 Hz filter (the reference receiver's; an app's own receiver
        # keeps its own width).
        z = render([(0, NOISE_S)], rng, 0.0, lead=0.0, tail=0.0, **kw)
        if f1a == "carriers":
            z = z + sum(carrier(len(z), PITCH + off, snr + 6.0, **kw) for off in (-60.0, 150.0, 220.0))
        elif f1a == "qrn":
            z = z + impulses(len(z), rng, snr, **kw)
        elif f1a in ("keyed-200", "keyed-wide"):
            runs = runs_for(" ".join([CQ_TEXT] * 6), 22, rng)
            other = render(runs, rng, 200.0, mark_hz=PITCH + (200.0 if f1a == "keyed-200" else 600.0),
                           lead=0.5, tail=0.0, **kw)[:len(z)]
            z[:len(other)] += other * 10 ** ((snr + 15.0) / 20)
            if f1a == "keyed-wide" and receiver is None:
                receiver = lambda r, p: CwReceiver(r, p, 1500.0)   # noqa: E731
        out, c, secs = decode(z, decoder=decoder, receiver=receiver, **kw)
        return len(out.replace(" ", "")) * 60.0 / secs, c, secs, out
    if text == "beacon":
        runs, sent = beacon_runs(2), " ".join([BEACON_TEXT] * 2)
    else:
        runs = runs_for(" ".join([text] * 2), wpm, rng if human else None)
        sent = " ".join([text] * 2)
    if f1a in ("heavy", "light"):
        runs = weighted(runs, wpm, 0.35 if f1a == "heavy" else -0.3)
        f1a = False
    if f1a == "two":
        # Another station 80 Hz above, 6 dB down, at its own speed: inside
        # AFC's reach and the decoder's references, so the decoder must
        # keep to the stronger one and not follow the other.
        z = render(runs, rng, snr, **kw)
        other = runs_for(" ".join([SECOND_TEXT] * 3), 26, rng)
        o = render(other, rng, 200.0, mark_hz=PITCH + 80, lead=0.3, tail=0.0, **kw)[:len(z)]
        z[:len(o)] += o * 10 ** (-6 / 20)
        out, c, secs = decode(z, decoder=decoder, receiver=receiver, **kw)
        return cer(out, sent), c, secs, out
    if f1a == "bookmark":
        # Old bookmark: dial 700 Hz under the space tone; the mark 400 Hz above it.
        z = render(runs, rng, snr, f1a=True, space_hz=PITCH, mark_hz=PITCH + 400, qsb=qsb, drift=drift, **kw)
    elif f1a == "beside":
        z = render(runs, rng, snr, f1a=True, space_hz=PITCH + 60 - 400, mark_hz=PITCH + 60, **kw)
        z = z + carrier(len(z), PITCH - 150, snr, **kw)
    elif f1a:
        z = render(runs, rng, snr, f1a=True, space_hz=PITCH - 400, mark_hz=PITCH, qsb=qsb, drift=drift, **kw)
    else:
        z = render(runs, rng, snr, qsb=qsb, drift=drift, **kw)
    out, c, secs = decode(z, decoder=decoder, receiver=receiver, **kw)
    return cer(out, sent), c, secs, out


def backend_us(decoder=None, seconds: float = 120.0, seed: int = 0) -> float:
    """CPU microseconds per second of signal for one channel of a
    skimmer: Decoder.feed_frames on ready-made frame amplitudes (a 20 WPM
    station 10 dB up, then as long of noise), in 75 ms blocks - the cost
    of each extra channel once a filter bank has made its amplitudes."""
    rng = np.random.default_rng(seed)
    frame = Decoder.FRAME_S
    runs = runs_for(" ".join([CQ_TEXT] * 20), 20, rng)
    key = key_envelope(runs, edge=0.015, rate=1 / frame)   # three frames of edge
    n = int(seconds / frame)
    key = np.concatenate([key, np.zeros(max(0, n - len(key)))])[:n]
    key[n // 2:] = 0.0
    noise = lambda: np.abs(rng.normal(1, 0.35, n))   # noqa: E731
    tone, quiet, loud = 3.0 * key + noise(), noise(), noise()
    dec = (decoder or Decoder)(None, freq=PITCH)
    block = int(round(BLOCK_S / frame))
    c0 = time.process_time()
    for i in range(0, n, block):
        dec.feed_frames(tone[i:i + block], quiet[i:i + block], loud[i:i + block])
    return 1e6 * (time.process_time() - c0) / seconds


def main(argv=None, receiver: Receiver | None = None) -> int:
    """The bench's command line. `receiver` replaces the reference one (an
    app measuring through its own demodulator, as NB's tools/cw_bench.py
    does)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--snr", default="0,3,6,9,12,15")
    ap.add_argument("--only", default="")
    ap.add_argument("--rate", type=float, default=FS, help="sample rate of the rendered cases (default 12000)")
    ap.add_argument("--show", action="store_true", help="print each decode")
    ap.add_argument("--recording", help="complex64 .npy at 12 kHz, the beacon's space tone at 0 Hz")
    ap.add_argument("--decoder", help="a .py file whose Decoder class to measure instead (e.g. an old cw.py)")
    a = ap.parse_args(argv)
    decoder = None
    rate = int(a.rate) if a.rate == int(a.rate) else a.rate
    if a.decoder:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cw_under_test", a.decoder)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # NB's own Decoder took feed(audio, t) and ignored t, so handing it
        # the rate there measures an old NB decoder as it was.
        decoder = mod.Decoder
    if a.recording:
        z = np.load(a.recording)
        for label, shift in (("mark on the pitch (new bookmark)", PITCH - 400), ("space on the pitch (old)", PITCH)):
            zz = (z * np.exp(2j * np.pi * shift * np.arange(len(z)) / FS)).astype(np.complex64)
            out, c, secs = decode(zz, decoder=decoder, receiver=receiver)
            n = round(secs / 37.9)
            print(f"{label}: cpu {1000 * c / secs:.2f} ms per s of audio\n  {out[:300]!r}")
            print(f"  CER vs {n} x beacon text: {cer(out, ' '.join([BEACON_TEXT] * n)):.3f} (approx: partial ends)")
        return 0
    snrs = [float(s) for s in a.snr.split(",")]
    names = [n for n in CASES if not a.only or n in a.only.split(",")]
    print(f"{'case':24s}" + "".join(f"{s:>7.1f}" for s in snrs) + f"   (CER %, mean of {a.seeds} seeds; SNR dB in 2.5 kHz;"
          " noise, carriers: characters per minute)")
    cpu = secs_tot = 0.0
    for name in names:
        row = []
        for snr in snrs:
            vals = []
            for seed in range(a.seeds):
                e, c, secs, out = run_case(name, snr, seed, decoder, rate=rate, receiver=receiver)
                cpu += c
                secs_tot += secs
                vals.append(e)
                if a.show:
                    print(f"  {name} {snr} {seed}: {e:.3f} {out!r}")
            row.append(float(np.mean(vals)) * (1 if not CASES[name][0] else 100))
        print(f"{name:24s}" + "".join(f"{v:7.1f}" for v in row), flush=True)
    print(f"decoder CPU: {1000 * cpu / secs_tot:.2f} ms per second of audio ({secs_tot:.0f} s decoded)")
    print(f"per channel without the front end (Decoder.feed_frames): {backend_us(decoder):.0f} us per second")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
