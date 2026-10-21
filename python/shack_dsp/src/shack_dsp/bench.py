"""What NR2 gains, what it costs, and whether the CW decoder should hear it.

    python3 -m shack_dsp.bench                 # every table, ~20 s
    python3 -m shack_dsp.bench --only cpu      # snr, cw, edges, cpu

**SNR improvement** is measured the way a suppression filter has to be: the
noisy audio decides the gains, and the same gains are applied to the clean
signal and to the noise separately (two non-key channels of one `Nr2`), so
the figure is what the filter did to each rather than a guess at which part
of the output is which. "Loss" is what it cost the signal (dB, negative is
quieter). Signals: synthetic speech (voiced syllables, a glottal pitch of
95-210 Hz under three moving formants, gaps between words) in an SSB
passband of 300-2700 Hz, and keyed CW at 20 WPM, 600 Hz, in a 500 Hz CW
filter. SNR is the signal's average power over the noise's in that band.

**CW decode** answers which audio the CW decoder (shack_cw) should hear: the
same keyed text in the same noise, decoded from the raw audio and from
NR2's, character error rate over three seeds. Skipped if shack_cw is not
installed.

**Edges**: a 1 kHz tone through NR2 cut into random packet sizes, and
switched on and off every 0.4 s; the worst sample-to-sample step is
compared with the tone's own (a click would be many times it), and the
sample count in and out must agree.

**CPU**: processor time per second of audio at the apps' rates.

A developer's measuring tool: English output, not translated.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from .nr2 import Nr2, Nr2Stream


def band_noise(rng, n, rate, lo, hi):
    """White Gaussian noise confined to lo..hi Hz, unit power."""
    spec = np.fft.rfft(rng.normal(size=n))
    f = np.fft.rfftfreq(n, 1 / rate)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.sqrt(np.mean(x * x))


def speech(rng, seconds, rate):
    """Speech-like audio: voiced syllables with moving pitch and formants."""
    n = int(seconds * rate)
    out = np.zeros(n)
    t = 0.0
    while t < seconds - 0.3:
        dur = rng.uniform(0.12, 0.32)
        i0, i1 = int(t * rate), min(n, int((t + dur) * rate))
        m = i1 - i0
        tt = np.arange(m) / rate
        f0 = rng.uniform(95, 210) * (1 + rng.uniform(-0.15, 0.15) * tt / dur)
        phase = 2 * np.pi * np.cumsum(f0) / rate
        formants = [rng.uniform(300, 800), rng.uniform(900, 2200), rng.uniform(2300, 2700)]
        seg = np.zeros(m)
        for k in range(1, 30):
            fk = k * f0
            amp = sum(np.exp(-((fk - F) / 120.0) ** 2) * w
                      for F, w in zip(formants, (1.0, 0.6, 0.25)))
            amp = amp * (fk < 2700)
            seg += amp * np.sin(k * phase)
        env = np.sin(np.pi * np.arange(m) / m) ** 0.6
        out[i0:i1] = seg * env
        t += dur + (rng.uniform(0.25, 0.6) if rng.random() < 0.25 else rng.uniform(0.03, 0.09))
    return out / np.sqrt(np.mean(out * out))


def cw(rng, seconds, rate, pitch=600.0, wpm=20.0, text=None):
    """Keyed CW with 5 ms edges; random letters, or `text` if shack_cw is there."""
    dit = 1.2 / wpm
    runs = []
    if text is not None:
        from shack_cw import runs as morse_runs
        runs = morse_runs(text, wpm)
    else:
        total = 0.0
        while total < seconds:
            for _ in range(int(rng.integers(1, 5))):
                d = dit * (3 if rng.random() < 0.4 else 1)
                runs += [(1, d), (0, dit)]
                total += d + dit
            runs.append((0, 2 * dit))
            total += 2 * dit
    n = int(seconds * rate) if text is None else int(sum(s for _, s in runs) * rate) + rate
    key = np.zeros(n)
    i = 0
    for on, s in runs:
        k = int(s * rate)
        if on:
            key[i:i + k] = 1.0
        i += k
        if i >= n:
            break
    edge = int(0.005 * rate)
    ramp = np.convolve(key, np.ones(edge) / edge, mode="same")
    x = ramp * np.sin(2 * np.pi * pitch * np.arange(n) / rate)
    return x / np.sqrt(np.mean(x * x))


def shadow(clean, noise, snr_db, rate, level):
    """(snr in, snr out, signal loss dB, noise reduction dB) for one run."""
    noise = noise * np.sqrt(np.mean(clean ** 2) / np.mean(noise ** 2)) * 10 ** (-snr_db / 20)
    noisy = clean + noise
    scale = 0.3 / np.max(np.abs(noisy))
    stack = np.stack([noisy, clean, noise], axis=1) * scale
    nr = Nr2(rate, channels=3, level=level, key=[0])
    y = nr.process(stack)
    d = nr.delay
    skip = int(2.0 * rate)             # past the noise estimate's start
    yc, yn = y[skip:, 1], y[skip:, 2]
    c, n = stack[skip - d:len(y) - d, 1], stack[skip - d:len(y) - d, 2]
    p = lambda v: float(np.mean(v * v))  # noqa: E731
    snr_in = 10 * np.log10(p(c) / p(n))
    snr_out = 10 * np.log10(p(yc) / p(yn))
    return snr_in, snr_out, 10 * np.log10(p(yc) / p(c)), 10 * np.log10(p(yn) / p(n))


def table_snr(rate=12_000, seeds=3):
    print(f"\nSNR improvement at {rate} Hz (mean of {seeds} seeds, 20 s each)")
    print(f"{'signal':8} {'level':>5} {'SNR in':>7} {'SNR out':>8} {'gain':>6} {'sig loss':>9} {'noise':>7}")
    rows = []
    for name in ("speech", "cw"):
        for level in (25, 50, 75, 100):
            for snr in (0, 6, 12):
                got = []
                for seed in range(seeds):
                    rng = np.random.default_rng(seed)
                    if name == "speech":
                        clean = speech(rng, 20, rate)
                        noise = band_noise(rng, len(clean), rate, 300, 2700)
                    else:
                        clean = cw(rng, 20, rate)
                        noise = band_noise(rng, len(clean), rate, 350, 850)
                    got.append(shadow(clean, noise, snr, rate, level))
                si, so, sl, nl = np.mean(got, axis=0)
                rows.append((name, level, snr, so - si))
                print(f"{name:8} {level:5d} {si:7.1f} {so:8.1f} {so - si:6.1f} {sl:9.1f} {nl:7.1f}")
    return rows


def _cer(got: str, want: str) -> float:
    got, want = " ".join(got.split()), " ".join(want.split())
    d = list(range(len(want) + 1))
    for i, g in enumerate(got, 1):
        prev, d[0] = d[0], i
        for j, w in enumerate(want, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (g != w))
    return d[len(want)] / max(1, len(want))


def table_cw_decode(rate=12_000, seeds=3, level=50):
    try:
        from shack_cw import Decoder
    except ImportError:
        print("\nCW decode: shack_cw is not installed, skipped")
        return None
    text = "CQ CQ DE GM4BRI GM4BRI IO75RL K"
    print(f"\nCW decode, character error rate, raw audio vs NR2 (level {level}), {rate} Hz")
    print(f"{'SNR dB':>6} {'raw':>6} {'NR2':>6}")
    out = []
    for snr in (-3, 0, 3, 6, 9):
        cer = {"raw": [], "nr2": []}
        for seed in range(seeds):
            rng = np.random.default_rng(100 + seed)
            clean = cw(rng, 0, rate, text=text)
            lead = np.zeros(int(2.0 * rate))
            clean = np.concatenate([lead, clean])
            noise = band_noise(rng, len(clean), rate, 350, 850)
            noise *= np.sqrt(np.mean(clean[len(lead):] ** 2)) * 10 ** (-snr / 20)
            noisy = (clean + noise) * 0.2
            nr = Nr2(rate, level=level)
            cleaned = np.concatenate([nr.process(noisy), np.zeros(nr.delay)])
            for name, audio in (("raw", noisy), ("nr2", cleaned)):
                dec = Decoder(freq=600.0)
                block = int(0.075 * rate)
                txt = ""
                for i in range(0, len(audio) - block + 1, block):
                    txt += "".join(e["text"] for e in dec.feed(audio[i:i + block].astype(np.float32), rate))
                for e in dec.feed(np.zeros(rate, dtype=np.float32), rate):
                    txt += e["text"]
                cer[name].append(_cer(txt, text))
        r, n = np.mean(cer["raw"]), np.mean(cer["nr2"])
        out.append((snr, r, n))
        print(f"{snr:6d} {r:6.2f} {n:6.2f}")
    return out


def table_edges(rate=12_000):
    rng = np.random.default_rng(7)
    seconds = 4.0
    n = int(seconds * rate)
    tone = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(n) / rate)
    noisy = tone + 0.02 * rng.normal(size=n)
    pcm = (np.round(noisy * 32767)).astype("<i2")
    st = Nr2Stream(rate, level=100, on=False)
    out = []
    i = 0
    toggles = 0
    next_toggle = int(0.4 * rate)
    while i < n:
        k = int(rng.integers(40, 600))
        if i >= next_toggle:
            st.on = not st.on
            toggles += 1
            next_toggle += int(0.4 * rate)
        out.append(st.feed(pcm[i:i + k].tobytes()))
        i += k
    st.on = False
    for _ in range(20):
        out.append(st.feed(np.zeros(240, dtype="<i2").tobytes()))
    y = np.frombuffer(b"".join(out), dtype="<i2").astype(np.float64)
    step_tone = np.max(np.abs(np.diff(pcm.astype(np.float64))))
    step_out = np.max(np.abs(np.diff(y[:n])))
    print(f"\nEdges at {rate} Hz: {toggles} on/off switches in random 40-600 sample packets")
    print(f"  samples in {n + 20 * 240}, out {len(y)}")
    print(f"  worst step: input {step_tone:.0f}, output {step_out:.0f} (ratio {step_out / step_tone:.2f})")
    return n + 20 * 240, len(y), step_out / step_tone


def table_cpu():
    print("\nCPU, processor time per second of audio (this host)")
    rng = np.random.default_rng(3)
    out = []
    for rate, ch in ((8000, 1), (12_000, 1), (24_000, 1), (24_000, 2), (48_000, 1)):
        x = rng.normal(0, 0.05, (rate * 20, ch))
        nr = Nr2(rate, channels=ch)
        t = time.process_time()
        nr.process(x if ch > 1 else x[:, 0])
        ms = (time.process_time() - t) / 20 * 1000
        out.append((rate, ch, ms))
        print(f"  {rate:6d} Hz x{ch}: {ms:5.1f} ms/s  (frame {nr.n}, latency {1000 * nr.latency_s:.0f} ms)")
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="snr,cw,edges,cpu")
    ap.add_argument("--rate", type=int, default=12_000)
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args(argv)
    only = set(a.only.split(","))
    if "snr" in only:
        table_snr(a.rate, a.seeds)
    if "cw" in only:
        table_cw_decode(a.rate, a.seeds)
    if "edges" in only:
        table_edges(a.rate)
    if "cpu" in only:
        table_cpu()


if __name__ == "__main__":
    main()
