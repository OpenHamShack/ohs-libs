"""Where a transmit audio path repeated or skipped samples: the sent audio
against what came back off the air.

    python3 -m shack_packet.slips sent.wav received.wav

Written for the IC-9700's USB codec (18/10/2026). Its transmissions failed
to decode here and in direwolf alike, and the received audio, lined up
against what was sent, jumps: a run of 16 samples (1/3 ms at 48 kHz) played
twice, now and then 48 skipped - 11 jumps in 1.2 s with the radio on
shack's shared USB controller, 30 on a controller of its own - while the
same audio over the radio's LAN stream came back with none. A jump moves
every later bit by 0.4 of a bit, which no HDLC receiver survives often. The
host's own stream was intact (usbmon), so the jumps are in the radio's
codec path, and the only way to see them is on the air. This counts them,
so every change to the path (rate, channels, host, hub, cable) is one keyed
over and one number instead of an afternoon with an FIR fit.

**How.** Both signals are band-passed to the AFSK tones (900-2600 Hz), and
for every 10 ms window of the sent audio, stepped 2.5 ms at a time, the
normalised correlation with the received audio is taken at every lag
within `max_drift_s` of the whole-signal best. A window on its own is
ambiguous - the preamble's flags repeat every 6.7 ms, and the tones every
fraction of a millisecond - so the lag is chosen for the whole over at
once: the path through the windows (Viterbi) that keeps the most
correlation, paying `penalty` for each change of lag. A real slip moves
every later window, so it pays for itself within a few windows; a
coincidence in one window does not. Measured on the 18/10 captures: the
LAN over came back with no slips, the USB over with 8 found where a
sample-exact fit (an FIR model of the radio and a search per slip) found 11
- two close together merge into one jump of their sum, and a slip in the
last ~30 ms of an over has too little audio after it to pay its penalty and
is missed. So read `count` as a lower bound and `drift_samples` (end lag
less start lag) as the honest total.

Steps smaller than `min_slip` samples are not slips but drift - a radio
whose clock really is a few hundred ppm off shows as single-sample steps -
and are counted in `drift_samples` only.

It needs the transmitted audio exactly, so it is a bench tool for a keyed
test whose audio was saved, not something a receiver can do.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np

#: The AFSK tones' band. Below it is the radio's sub-audible mess and the
#: discriminator's DC; above it, noise.
BAND_HZ = (900.0, 2600.0)


@dataclass
class Slip:
    at_s: float          # where, in the sent audio
    samples: int         # + played again (the received audio is longer), - skipped


@dataclass
class SlipReport:
    rate: int
    delay_samples: int               # received lag at the start of the over
    drift_samples: int               # end lag less start lag: every slip and drift
    match: float                     # median correlation along the chosen path, 0-1
    seconds: float                   # how much sent audio was compared
    slips: list[Slip] = field(default_factory=list)

    @property
    def per_second(self) -> float:
        return len(self.slips) / self.seconds if self.seconds else 0.0

    def summary(self) -> str:
        sizes = ", ".join(f"{s.samples:+d}@{s.at_s * 1000:.0f}ms" for s in self.slips) or "none"
        return (f"{len(self.slips)} slips in {self.seconds:.2f} s ({self.per_second:.1f}/s); "
                f"drift {self.drift_samples:+d} samples ({self.drift_samples / self.rate * 1000:+.2f} ms, "
                f"{self.drift_samples / max(1, self.seconds * self.rate) * 1e6:+.0f} ppm); "
                f"match {self.match:.2f}; delay {self.delay_samples} samples\n  {sizes}")


def _band(x: np.ndarray, rate: float) -> np.ndarray:
    spectrum = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / rate)
    spectrum[(f < BAND_HZ[0]) | (f > BAND_HZ[1])] = 0
    return np.fft.irfft(spectrum, len(x))


def find_slips(sent, received, rate: int, *, window_s: float = 0.01, hop_s: float = 0.0025,
               max_drift_s: float | None = None, max_jump_s: float = 0.0015, penalty: float = 3.0,
               min_slip: int | None = None) -> SlipReport:
    """Line `received` up against `sent` (both at `rate`) and say where it jumped.

    `max_drift_s` is how far the lag may wander from where it starts: by
    default 2 ms plus 1 % of the over, which holds the 9700's worst (30
    slips of 16 samples in 1.2 s, 10 ms). `max_jump_s` is the biggest single
    jump looked for (1.5 ms: a whole 1 ms USB packet and change).
    `min_slip` defaults to 1/12 ms (4 samples at 48 kHz).
    """
    s = _band(np.asarray(sent, dtype=np.float64), rate)
    r = _band(np.asarray(received, dtype=np.float64), rate)
    if max_drift_s is None:
        max_drift_s = 0.002 + 0.01 * len(s) / rate
    if min_slip is None:
        min_slip = max(2, round(rate / 12000))
    n = 1 << int(np.ceil(np.log2(len(s) + len(r))))
    whole = np.fft.irfft(np.fft.rfft(r, n) * np.conj(np.fft.rfft(s, n)), n)[:len(r)]
    k0 = int(np.argmax(np.abs(whole)))
    width, hop = int(window_s * rate), max(1, int(hop_s * rate))
    span, jump = int(max_drift_s * rate), max(1, int(max_jump_s * rate))
    lags = np.arange(k0 - span, k0 + span + 1)
    # Pad so that every lag of every window reads something (silence beyond the ends).
    padded = np.concatenate([np.zeros(span), r, np.zeros(span + len(s))])
    starts = [at for at in range(0, len(s) - width, hop) if np.std(s[at:at + width]) > 1e-4]
    if not starts:
        return SlipReport(rate, k0, 0, 0.0, 0.0)
    ones = np.ones(width)
    q = np.empty((len(starts), len(lags)))
    for i, at in enumerate(starts):
        seg = s[at:at + width]
        seg = seg / np.linalg.norm(seg)
        rr = padded[span + at + lags[0]: span + at + lags[-1] + width]
        energy = np.sqrt(np.maximum(np.convolve(rr * rr, ones, "valid"), 0.0)) + 1e-9
        q[i] = np.abs(np.correlate(rr, seg, "valid")) / energy

    # Viterbi over lags: stay for free, or jump by up to `jump` for `penalty`
    # (a little more for a bigger jump, so a jump is never split in two).
    count = len(lags)
    index = np.arange(count)
    score = q[0].copy()
    back = np.empty(q.shape, dtype=np.int32)
    back[0] = index
    for i in range(1, len(q)):
        best, came = score.copy(), index.copy()
        for d in range(1, jump + 1):
            cost = penalty + d / jump
            up = np.full(count, -np.inf)
            up[d:] = score[:-d] - cost
            down = np.full(count, -np.inf)
            down[:-d] = score[d:] - cost
            for cand, src in ((up, index - d), (down, index + d)):
                better = cand > best
                best = np.where(better, cand, best)
                came = np.where(better, src, came)
        back[i] = came
        score = best + q[i]
    path = [int(np.argmax(score))]
    for i in range(len(q) - 1, 0, -1):
        path.append(int(back[i][path[-1]]))
    path.reverse()
    lag = lags[path]

    slips = []
    for i in range(1, len(lag)):
        step = int(lag[i] - lag[i - 1])
        if abs(step) >= min_slip:
            slips.append(Slip(starts[i] / rate, step))
    match = float(np.median(q[np.arange(len(q)), path]))
    seconds = (starts[-1] + width - starts[0]) / rate
    return SlipReport(rate, int(lag[0]), int(lag[-1] - lag[0]), match, seconds, slips)


def apply_slips(audio: np.ndarray, slips, rate: int) -> np.ndarray:
    """The fault as modelled: at each (seconds, n), n > 0 plays the last n
    samples again and n < 0 skips -n. For the tests and the bench."""
    out, done = [], 0
    for at_s, n in sorted(slips):
        at = int(round(at_s * rate))
        if at >= len(audio) or at < done:
            continue
        out.append(audio[done:at])
        if n > 0:
            out.append(audio[max(0, at - n):at])
            done = at
        else:
            done = at - n
    out.append(audio[done:])
    return np.concatenate(out)


def _load(path: str) -> tuple[int | None, np.ndarray]:
    if path.endswith(".npy"):
        return None, np.load(path).astype(np.float64)
    from .bench import read_wav
    return read_wav(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sent", help="what was transmitted (.wav, or .npy at --rate)")
    parser.add_argument("received", help="what came back, FM-demodulated (.wav, or .npy at --rate)")
    parser.add_argument("--rate", type=int, default=48000, help="for .npy files (default 48000)")
    args = parser.parse_args(argv)
    r1, sent = _load(args.sent)
    r2, received = _load(args.received)
    rate = r1 or r2 or args.rate
    if (r1 or rate) != (r2 or rate):
        parser.error(f"the two files are at different rates ({r1} and {r2}); resample one first")
    print(find_slips(sent, received, rate).summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
