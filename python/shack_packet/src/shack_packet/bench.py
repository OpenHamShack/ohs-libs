"""Measures the receiver: how many frames it gets out of noise, against
direwolf where direwolf is installed, and what it costs.

    python3 -m shack_packet.bench                    # every table, ~3 min
    python3 -m shack_packet.bench --only ramp,cpu    # ramp, fx25, synth, cpu, slips
    python3 -m shack_packet.bench --wav track2.wav   # a recording, here and in atest

`ramp` is the comparison everyone quotes: direwolf's `gen_packets -n 30`
noise ramp at five amplitudes (10, 25, 50, 100, 150 %), decoded by this
receiver and by direwolf's `atest`. `fx25` is FX.25 against plain AX.25 in
our own noise, and direwolf's ramp again with `gen_packets -X`.
`synth` is our own set - 40 frames at each of 3, 4 and 5 dB SNR (in a 6 kHz
band) under twist of -9, -6, 0, +6 and +9 dB an octave - which says what the
ramp cannot: the ramp is flat audio, and real radios are not. `cpu` is
process time per second of audio per channel. `slips` is a transmit path
that repeats samples, as the IC-9700's USB codec does (slips.py), plain
and FX.25.

direwolf is a development tool here, never a dependency: without
`gen_packets` and `atest` the ramp tables say so and the rest still runs.
Temporary audio goes under ~/build, not /tmp.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from . import afsk, ax25, demod

AMPLITUDES = (10, 25, 50, 100, 150)
TWISTS = (-9, -6, 0, 6, 9)
SNRS = (3, 4, 5)


def read_wav(path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    if channels > 1:
        data = data[::channels]
    return rate, data.astype(np.float64) / 32768.0


def write_wav(path, audio: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(afsk.pcm16(audio))


def decode(audio: np.ndarray, rate: int, **kw) -> list[demod.Received]:
    rx = demod.Receiver(rate, **kw)
    return rx.feed(audio) + rx.flush()


def decode_wav(path, **kw) -> list[demod.Received]:
    rate, audio = read_wav(path)
    return decode(audio, rate, **kw)


def atest_count(path, *args) -> int | None:
    """How many frames direwolf's atest decodes from a wav file."""
    if not shutil.which("atest"):
        return None
    run = subprocess.run(["atest", *args, str(path)], capture_output=True, timeout=600, check=False)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", run.stdout.decode("latin-1", "replace"))
    for line in plain.splitlines():
        match = re.match(r"^(\d+) from ", line.strip())
        if match:
            return int(match.group(1))
    return 0


def gen_packets(path, *args) -> bool:
    if not shutil.which("gen_packets"):
        return False
    subprocess.run(["gen_packets", *args, "-o", str(path)], capture_output=True, check=True, timeout=120)
    return True


def workdir() -> tempfile.TemporaryDirectory:
    base = Path.home() / "build"
    base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="shack-packet-bench-", dir=base)


# -- our own noise and twist --------------------------------------------------


def test_frames(n: int, seed: int) -> list[bytes]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        text = "".join(chr(int(c)) for c in rng.integers(32, 127, int(rng.integers(20, 90))))
        out.append(ax25.Frame.from_monitor(f"GM0ABC-{i % 16}>APDW17,WIDE1-1:{text}").encode())
    return out


def tilt(x: np.ndarray, rate: int, db_per_octave: float) -> np.ndarray:
    """A spectral slope through the band, 0 dB at 1600 Hz, flat below 300 Hz
    and above 4 kHz: what a radio's pre- or de-emphasis does to the tones
    when the other end did not undo it."""
    if not db_per_octave:
        return x
    spectrum = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1.0 / rate)
    gain = (np.clip(freq, 300.0, min(4000.0, rate / 2 - 1)) / 1600.0) ** (db_per_octave / 6.0206)
    return np.fft.irfft(spectrum * gain, len(x))


def synthetic(n: int = 40, snr_db: float = 4.0, rate: int = 12_000, twist: float = 0.0,
              seed: int = 0, baud_error: float = 0.0, gap_s: float = 0.05,
              fx25_mode=None) -> tuple[np.ndarray, list[bytes]]:
    """`n` frames, each its own transmission (100 ms TXDELAY), white noise at
    `snr_db` below the signal's power in a 6 kHz band whatever the rate."""
    rng = np.random.default_rng(seed + 1000)
    frames = test_frames(n, seed)
    parts, signal = [], []
    for frame in frames:
        mod = afsk.Modulator(rate, 0.5, baud=afsk.BAUD * (1.0 + baud_error))
        audio = mod.bits(afsk.transmission_bits([frame], fx25_mode=fx25_mode, txdelay_ms=100, tail_ms=20))
        parts += [np.zeros(int(rate * gap_s)), audio]
        signal.append(audio)
    x = tilt(np.concatenate(parts + [np.zeros(int(rate * 0.1))]), rate, twist)
    power = float(np.mean(np.concatenate(signal) ** 2))
    noise = np.sqrt(power / 10 ** (snr_db / 10.0) * rate / 12_000.0)
    return x + rng.normal(0.0, noise, len(x)), frames


# -- the tables ---------------------------------------------------------------


def table_ramp(rate: int = 12_000) -> None:
    print(f"\ndirewolf's noise ramp: gen_packets -n 30 -r {rate}, five amplitudes")
    with workdir() as tmp:
        if not gen_packets(Path(tmp) / "probe.wav", "-r", str(rate)):
            print("  gen_packets is not installed (apt install direwolf); skipped")
            return
        ours = theirs = best = 0
        print(f"  {'amplitude':>9}  {'here':>5}  {'atest':>5}  {'atest -P E+':>11}")
        for amp in AMPLITUDES:
            path = Path(tmp) / f"ramp{amp}.wav"
            gen_packets(path, "-r", str(rate), "-n", "30", "-a", str(amp))
            got = len(decode_wav(path))
            dw, dwp = atest_count(path), atest_count(path, "-P", "E+")
            ours, theirs, best = ours + got, theirs + (dw or 0), best + (dwp or 0)
            print(f"  {amp:>8}%  {got:>5}  {dw:>5}  {dwp:>11}")
        print(f"  {'total':>9}  {ours:>5}  {theirs:>5}  {best:>11}   (of {30 * len(AMPLITUDES)})")


def table_fx25(rate: int = 12_000) -> None:
    print(f"\nFX.25 against plain AX.25 in our noise, {rate} Hz, 40 frames a cell, no twist:")
    print("  frames decoded (of them, reported as decoded through FX.25)")
    snrs = (0, 1, 2, 3, 4)
    print(f"  {'check bytes':>11}  " + "  ".join(f"{s:>5} dB" for s in snrs))
    for mode in (None, 16, 32, 64):
        row = []
        for snr in snrs:
            audio, frames = synthetic(40, snr, rate, 0, seed=snr, fx25_mode=mode)
            got = decode(audio, rate)
            row.append(f"{len({g.frame for g in got} & set(frames)):>3} ({sum(g.fx25 is not None for g in got):>2})")
        print(f"  {mode or 'plain':>11}  " + "  ".join(row))

    print(f"\nFX.25 on direwolf's noise ramp: gen_packets -n 30 -X N -r {rate}, amplitude 50 %")
    with workdir() as tmp:
        if not gen_packets(Path(tmp) / "probe.wav", "-r", str(rate)):
            print("  gen_packets is not installed (apt install direwolf); skipped")
            return
        print(f"  {'check bytes':>11}  {'here':>5}  {'atest':>5}")
        for check in (0, 16, 32, 64):
            path = Path(tmp) / f"fx{check}.wav"
            args = ["-r", str(rate), "-n", "30"] + (["-X", str(check)] if check else [])
            gen_packets(path, *args)
            got = decode_wav(path)
            name = str(check) if check else "plain"
            print(f"  {name:>11}  {len(got):>5}  {atest_count(path):>5}")


def table_synth(rate: int = 12_000, diversity: bool = True) -> None:
    print(f"\nour noise and twist, {rate} Hz, 40 frames a cell"
          + ("" if diversity else ", one demodulator"))
    print(f"  {'twist dB/oct':>12}  " + "  ".join(f"{s:>2} dB" for s in SNRS))
    total = 0
    for twist in TWISTS:
        row = []
        for snr in SNRS:
            audio, frames = synthetic(40, snr, rate, twist, seed=int(snr * 10 + twist))
            got = {g.frame for g in decode(audio, rate, diversity=diversity)}
            row.append(len(got & set(frames)))
        total += sum(row)
        print(f"  {twist:>+12}  " + "  ".join(f"{r:>5}" for r in row))
    print(f"  {'total':>12}  {total} of {40 * len(TWISTS) * len(SNRS)}")


def table_slips(rate: int = 48_000, overs: int = 10) -> None:
    """A transmit path that repeats 16 samples now and then, as the
    IC-9700's USB codec did (slips.py): what survives it, plain and FX.25,
    here and in direwolf. 3 frames an over, `overs` overs a cell, slips at
    random (Poisson) at 9 a second (the 9700 on shack's shared controller)
    and 25 (on its own); one in twelve is a skipped 48 instead. Through a
    300-3000 Hz path, no noise: the slips alone.

    Measured on shack 18/10/2026 (of 30): at 9/s plain 6 here, 1 atest,
    3 atest -P E+ -F 2; FX.25 with 16/32/64 check bytes 13/16/21 here and
    1-3 in atest (5-11 with -F 2). At 25/s nothing passes more than 4.
    So FX.25 does not make such a path usable: every slip moves the rest
    of the frame by 0.4 bit, and a receiver whose bit clock rides that out
    (ours, mostly) still loses the bits around it, FX.25 or not."""
    from .slips import apply_slips
    print(f"\nmodelled transmit slips (+16 samples, 1 in 12 -48) at {rate} Hz, {overs} overs of 3 frames a cell")
    print(f"  {'slips/s':>7}  {'check bytes':>11}  {'here':>5}  {'(fx25)':>6}  {'atest':>5}  {'atest -P E+ -F 2':>16}")
    rng = np.random.default_rng(7)
    with workdir() as tmp:
        for per_s in (9.0, 25.0):
            for mode in (None, 16, 32, 64):
                ours = fx = dw = dwf = 0
                for over in range(overs):
                    frames = test_frames(3, over)
                    audio = afsk.modulate(frames, rate=rate, fx25_mode=mode, txdelay_ms=300)
                    put, at = [], 0.0
                    while True:
                        at += rng.exponential(1.0 / per_s)
                        if at * rate >= len(audio):
                            break
                        put.append((at, -48 * rate // 48_000 if rng.random() < 1 / 12 else 16 * rate // 48_000))
                    spectrum = np.fft.rfft(apply_slips(audio, put, rate))
                    freq = np.fft.rfftfreq(2 * len(spectrum) - 2, 1.0 / rate)
                    spectrum[(freq < 300) | (freq > 3000)] = 0
                    heard = np.fft.irfft(spectrum)
                    got = [g for g in decode(heard, rate) if g.frame in frames]
                    ours += len(got)
                    fx += sum(g.fx25 is not None for g in got)
                    path = Path(tmp) / "slips.wav"
                    write_wav(path, heard / max(1e-9, float(np.max(np.abs(heard)))) * 0.8, rate)
                    dw += atest_count(path, "-B", "1200") or 0
                    dwf += atest_count(path, "-B", "1200", "-P", "E+", "-F", "2") or 0
                print(f"  {per_s:>7.0f}  {mode or 'plain':>11}  {ours:>5}  {fx:>6}  {dw:>5}  {dwf:>16}   (of {3 * overs})")


def table_cpu() -> None:
    print("\nCPU per second of audio, one channel (process time; noise only, the worst case)")
    rng = np.random.default_rng(5)
    for rate in (12_000, 48_000):
        noise = rng.normal(0.0, 0.1, rate * 20)
        for diversity in (True, False):
            rx = demod.Receiver(rate, diversity=diversity)
            started = time.process_time()
            rx.feed(noise)
            took = time.process_time() - started
            label = "two demodulators" if diversity else "one demodulator"
            print(f"  {rate:>6} Hz, {label:<16}  {took / 20 * 1000:6.1f} ms  "
                  f"({took / 20 * 100:.1f} % of a core; 16 channels {took / 20 * 1600:.0f} %)")


def table_wav(paths) -> None:
    """Recordings: the WA8LMF TNC Test CD's tracks, or anything off the
    air. Never committed (only generated audio is); kept under ~/build."""
    print("\nrecordings (left channel)")
    print(f"  {'file':<28}  {'here':>5}  {'one demod':>9}  {'atest':>5}  {'atest -P E+':>11}")
    for path in paths:
        rate, audio = read_wav(path)
        here = len(decode(audio, rate))
        one = len(decode(audio, rate, diversity=False))
        dw, dwp = atest_count(path), atest_count(path, "-P", "E+")
        print(f"  {Path(path).name:<28}  {here:>5}  {one:>9}  {dw!s:>5}  {dwp!s:>11}")


TABLES = {"ramp": table_ramp, "fx25": table_fx25, "synth": table_synth, "cpu": table_cpu,
          "slips": table_slips}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default=",".join(TABLES), help="comma-separated: " + ", ".join(TABLES))
    parser.add_argument("--wav", nargs="+", help="decode these recordings instead (and with atest if installed)")
    args = parser.parse_args(argv)
    print(f"shack_packet bench on {os.uname().nodename}")
    if args.wav:
        table_wav(args.wav)
        return 0
    for name in args.only.split(","):
        TABLES[name.strip()]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
