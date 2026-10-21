"""NR2: spectral noise reduction for receive audio, in the manner of
PowerSDR/Thetis NR2.

Brian asked for NR2 on the Icom and Flex apps (10/10/2026). Both radios have
noise reduction of their own, but it lives in the radio and sounds like the
radio; NR2 is the one an SDR operator knows by name, and running it on the
server puts it in front of every client - the web page, Remote on a TV, the
desktop - with no DSP in any of them.

**What it is.** A short-time spectral estimator (Ephraim and Malah's
log-spectral-amplitude MMSE gain, 1985), which is what NR2 is:

- the audio is cut into frames of about 21 ms, half-overlapped, under a
  square-root Hann window, and each frame's spectrum is computed;
- the noise in each frequency bin is estimated by **minimum statistics**
  (Martin 2001): a smoothed power spectrum, and the smallest value it has
  taken in the last ~1.5 s, scaled up by the bias a minimum has. It needs
  no voice-activity detector, so it follows a noise floor that changes
  under a signal that never stops;
- the a-priori SNR comes from the **decision-directed** rule (98 % the last
  frame's cleaned estimate, 2 % this frame's raw excess), which is what
  keeps the residual noise from turning into "musical" tones, and then a
  second step (Plapous, Marro and Scalart's TSNR, 2006) re-estimates it from
  the first step's gain. The decision-directed rule alone lags a frame or
  three behind a signal that starts suddenly, which shaved the front off
  every CW element (a dit is five frames at 20 WPM); the second step gave
  back about 1 dB on speech and on CW;
- the LSA gain is applied, never below a **gain floor**, and the frame is
  windowed again and overlap-added back. With every gain 1 the output is
  the input exactly, delayed by half a frame.

**Strength (`level`, 0-100)** moves two things together: the gain floor,
-8 dB at 0 to -26 dB at 100 (how far noise is pushed down), and an
over-estimate of the noise, 1x to 2x (how readily a bin is called noise).
50 is a floor of -17 dB, about where Thetis's default sits by ear.

**What it costs.** One rfft and one irfft per hop per channel and a dozen
vector operations over the bins: about 2 % of one ohs-dev core per second
of 12 kHz mono (`python -m shack_dsp.bench`). Latency is one frame: half a
frame of delay plus up to half a frame waiting for the next hop.

**What it does to a steady carrier.** Minimum statistics calls anything
that has not dropped for 1.5 s noise: an unkeyed carrier fades out over a
couple of seconds, as it does in Thetis. Keyed CW, speech and data all have
gaps and are unaffected by that.

**Any rate, any number of channels.** The frame is the power of two nearest
21 ms at the given rate (256 at 12 kHz, 512 at 24 kHz). Several channels
share one gain per bin, worked out from the channels chosen as the key
(by default all of them, averaged in power): a stereo stream keeps its
image, and the bench uses a non-key channel to measure what the gains do
to the clean signal and the noise separately.

`Nr2` is the float core; `Nr2Stream` wraps it for the apps' int16 PCM and
switches it on and off without a click or a lost sample.
"""

from __future__ import annotations

import math

import numpy as np

#: The frame length aimed at, in seconds. 21 ms is a 256-point frame at
#: 12 kHz: fine enough in frequency (47 Hz bins) for speech harmonics and
#: CW, short enough that a keyed element is several frames.
FRAME_S = 0.0213

#: The window minimum statistics looks back over, in seconds, as Martin's
#: paper suggests for speech (it must outlast a word, or speech becomes
#: "noise"), in this many sub-windows.
MIN_WINDOW_S = 1.5
SUBWINDOWS = 8

#: Smoothing of the power spectrum minimum statistics tracks (per frame).
#: 0.85 (Martin's usual figure) did not fall far enough in the 60 ms gap
#: between two dits, so in a run of fast CW the tone's own bins were taken
#: for noise and the signal lost 7-12 dB; at 0.7 it loses 1-2 dB at 6 dB
#: SNR and gains 3-4 dB more SNR than at 0.85, with speech unchanged.
ALPHA_SMOOTH = 0.7

#: The decision-directed weight on the previous frame's estimate.
ALPHA_DD = 0.98

#: A minimum is below the mean it was taken from. For a periodogram
#: smoothed by ALPHA_SMOOTH and a minimum over ~140 frames this factor
#: brings it back to the mean of stationary noise; measured on white and
#: pink noise at 8, 12, 24 and 48 kHz (tests/test_nr2.py holds it within
#: 2 dB).
MIN_BIAS = 3.3

#: The smallest a-priori SNR allowed (-25 dB): below it the gain is the
#: floor anyway, and a bound keeps the LSA's exponential integral finite.
XI_MIN = 10 ** (-25 / 10)


#: Strength 0 and 100: gain floor in dB and the noise over-estimate.
FLOOR_DB = (-8.0, -26.0)
OVERSUB = (1.0, 2.0)

#: How long switching on or off takes to fade, seconds.
RAMP_S = 0.05


class Nr2Misuse(ValueError):
    """A caller's mistake (a rate of nothing, the wrong channel count): for
    the developer reading a traceback, never a sentence shown to a person,
    which is why it is not translated. Nothing an operator does reaches it:
    the apps build the core from the radio's own format."""


def frame_for(rate: float) -> int:
    """The power-of-two frame nearest FRAME_S at `rate` (64 at least)."""
    n = max(64, int(round(rate * FRAME_S)))
    return 1 << int(round(math.log2(n)))


def expint_e1(x: np.ndarray) -> np.ndarray:
    """The exponential integral E1(x) for x > 0, to about 1e-4 relative.

    numpy has none and scipy is not worth shipping for one function, so the
    two Abramowitz and Stegun approximations: 5.1.53 below 1 (a polynomial
    in x plus -ln x), 5.1.56 above (a rational function times e^-x / x).
    """
    x = np.maximum(np.asarray(x, dtype=np.float64), 1e-10)
    small = x < 1.0
    out = np.empty_like(x)
    xs = x[small]
    out[small] = (-np.log(xs) - 0.57721566 + xs * (0.99999193 + xs * (-0.24991055 + xs * (
        0.05519968 + xs * (-0.00976004 + xs * 0.00107857)))))
    xl = x[~small]
    out[~small] = (np.exp(-xl) / xl) * (
        (xl * xl + 2.334733 * xl + 0.250621) / (xl * xl + 3.330657 * xl + 1.681534))
    return out


def strength(level: float) -> tuple[float, float]:
    """(gain floor as an amplitude, noise over-estimate) for level 0-100."""
    t = min(max(float(level), 0.0), 100.0) / 100.0
    floor_db = FLOOR_DB[0] + t * (FLOOR_DB[1] - FLOOR_DB[0])
    return 10 ** (floor_db / 20), OVERSUB[0] + t * (OVERSUB[1] - OVERSUB[0])


class Nr2:
    """The float core: samples in, the same samples cleaned out, delayed.

    `process(x)` takes any number of samples - shape (n,) for one channel or
    (n, channels) - and returns however many are ready, in the same shape.
    The output does not depend on how the input was cut into blocks. It is
    `delay` samples behind the input.

    `mix` (0-1) blends the gain towards 1: 0 is the input exactly (delayed),
    1 is full NR2. Setting `mix_target` moves it there a frame at a time over
    RAMP_S, which is how `Nr2Stream` switches on and off smoothly;
    `frames_at_target` counts the frames since it arrived.
    """

    def __init__(self, rate: float, channels: int = 1, level: float = 50.0,
                 key: list[int] | None = None):
        if rate <= 0:
            raise Nr2Misuse("rate must be positive")
        if channels < 1:
            raise Nr2Misuse("channels must be 1 or more")
        self.rate = float(rate)
        self.channels = int(channels)
        self.key = list(range(self.channels)) if key is None else list(key)
        self.n = frame_for(rate)
        self.hop = self.n // 2
        self.delay = self.n - self.hop
        # sqrt of a periodic Hann: analysis times synthesis is Hann, whose
        # half-overlapped copies add to exactly 1.
        hann = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.n) / self.n)
        self.window = np.sqrt(hann)
        bins = self.n // 2 + 1
        hops_per_s = self.rate / self.hop
        self.sub_len = max(1, int(round(MIN_WINDOW_S * hops_per_s / SUBWINDOWS)))
        self._smooth = np.zeros(bins)
        self._sub_min = np.full(bins, np.inf)
        self._mins = np.full((SUBWINDOWS, bins), np.inf)
        self._sub_count = 0
        self._noise = np.zeros(bins)
        self._prev_snr = np.ones(bins)   # G^2 * gamma of the last frame
        self._frames = 0
        self._in = np.zeros((self.delay, self.channels))
        self._acc = np.zeros((self.n, self.channels))
        self.mix = 1.0
        self.mix_target = 1.0
        self.frames_at_target = 0
        self._mix_step = self.hop / (self.rate * RAMP_S)
        self.level = level

    # -- settings -----------------------------------------------------------

    @property
    def level(self) -> float:
        return self._level

    @level.setter
    def level(self, value: float) -> None:
        self._level = min(max(float(value), 0.0), 100.0)
        self._floor, self._oversub = strength(self._level)

    @property
    def latency_s(self) -> float:
        """What the stream adds at worst: the delay, and a hop of waiting."""
        return self.n / self.rate

    @property
    def noise(self) -> np.ndarray:
        """The current noise power estimate per bin (for tests and bench)."""
        return self._noise.copy()

    # -- the work -----------------------------------------------------------

    def process(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        mono = x.ndim == 1
        if mono:
            x = x[:, None]
        if x.shape[1] != self.channels:
            raise Nr2Misuse(f"expected {self.channels} channels, got {x.shape[1]}")
        buf = np.concatenate([self._in, x]) if len(self._in) else x
        out = []
        start = 0
        while len(buf) - start >= self.n:
            frame = buf[start:start + self.n]
            self._acc += self._frame(frame)
            out.append(self._acc[:self.hop].copy())
            self._acc[:-self.hop] = self._acc[self.hop:]
            self._acc[-self.hop:] = 0.0
            start += self.hop
        self._in = buf[start:].copy()
        y = np.concatenate(out) if out else np.zeros((0, self.channels))
        return y[:, 0] if mono else y

    def _frame(self, frame: np.ndarray) -> np.ndarray:
        spec = np.fft.rfft(frame * self.window[:, None], axis=0)
        power = np.mean(np.abs(spec[:, self.key]) ** 2, axis=1)
        gain = self._gain(power)
        if self.mix != self.mix_target:
            self.frames_at_target = 0
            if self.mix < self.mix_target:
                self.mix = min(self.mix_target, self.mix + self._mix_step)
            else:
                self.mix = max(self.mix_target, self.mix - self._mix_step)
        else:
            self.frames_at_target += 1
        if self.mix < 1.0:
            gain = 1.0 - self.mix * (1.0 - gain)
        return np.fft.irfft(spec * gain[:, None], n=self.n, axis=0) * self.window[:, None]

    def _gain(self, power: np.ndarray) -> np.ndarray:
        self._track_noise(power)
        noise = np.maximum(self._noise * self._oversub, 1e-20)
        gamma = power / noise
        xi = ALPHA_DD * self._prev_snr + (1 - ALPHA_DD) * np.maximum(gamma - 1.0, 0.0)
        xi = np.maximum(xi, XI_MIN)
        gain = self._lsa(xi, gamma)
        self._prev_snr = gain * gain * gamma
        # The second step: the a-priori SNR again, from the first gain.
        xi = np.maximum(gain * gain * gamma, XI_MIN)
        gain = self._lsa(xi, gamma)
        return np.clip(gain, self._floor, 1.0)

    @staticmethod
    def _lsa(xi: np.ndarray, gamma: np.ndarray) -> np.ndarray:
        v = xi * gamma / (1.0 + xi)
        return np.minimum(xi / (1.0 + xi) * np.exp(0.5 * expint_e1(v)), 1.0)

    def _track_noise(self, power: np.ndarray) -> None:
        """Minimum statistics: the least of a smoothed spectrum over the
        last MIN_WINDOW_S, kept as SUBWINDOWS rolling sub-minima so the
        window slides a sub-window at a time without storing every frame."""
        if self._frames == 0:
            self._smooth[:] = power
        else:
            self._smooth = ALPHA_SMOOTH * self._smooth + (1 - ALPHA_SMOOTH) * power
        self._frames += 1
        np.minimum(self._sub_min, self._smooth, out=self._sub_min)
        self._sub_count += 1
        if self._sub_count >= self.sub_len:
            self._mins = np.roll(self._mins, 1, axis=0)
            self._mins[0] = self._sub_min
            self._sub_min = np.full_like(self._sub_min, np.inf)
            self._sub_count = 0
        least = np.minimum(self._mins.min(axis=0), self._sub_min)
        if self._frames < self.sub_len * SUBWINDOWS:
            # Until a whole window has been seen the minimum is of too few
            # frames and the bias factor would overstate it; the start of the
            # stream is assumed to be noise, as every NR2 does.
            self._noise = np.minimum(self._smooth, least * MIN_BIAS)
        else:
            self._noise = least * MIN_BIAS


class Nr2Stream:
    """NR2 for an app's receive audio: signed 16-bit little-endian PCM in,
    the same format out, switchable while it runs.

    Off, it is a straight pass-through: no delay, no work. Switching on
    primes the core with the last half-frame of audio already passed
    through, so its first output joins the pass-through sample for sample,
    and fades the gain in over RAMP_S; the stream then runs half a frame
    behind, which a listener sees once as a short pause in arrival and
    never as a gap in the audio. Switching off fades the gain out, and once
    it is 1 everywhere the core's output *is* its input, so the audio
    still inside it is handed back raw and the pass-through resumes - no
    sample lost, none repeated, nothing for the ear to catch.
    """

    def __init__(self, rate: int, channels: int = 1, level: float = 50.0, on: bool = False):
        self.rate = int(rate)
        self.channels = int(channels)
        self._level = float(level)
        self._want = bool(on)
        self._core: Nr2 | None = None
        n = frame_for(self.rate)
        self.latency_ms = int(round(1000 * n / self.rate))
        #: The last few raw samples passed through, to prime the core with.
        self._hist = np.zeros((n, self.channels))
        #: Raw samples the core has been given but not yet returned.
        self._pending = np.zeros((0, self.channels))
        self._discard = 0

    @property
    def on(self) -> bool:
        return self._want

    @on.setter
    def on(self, value: bool) -> None:
        self._want = bool(value)

    @property
    def level(self) -> float:
        return self._level

    @level.setter
    def level(self, value: float) -> None:
        self._level = min(max(float(value), 0.0), 100.0)
        if self._core is not None:
            self._core.level = self._level

    @property
    def active(self) -> bool:
        """Whether the core is running (on, or still fading out)."""
        return self._core is not None

    def feed(self, pcm: bytes) -> bytes:
        """Some audio in; whatever is ready out (possibly none while
        switching on). Every input sample comes out exactly once."""
        frame = 2 * self.channels
        if len(pcm) % frame:
            pcm = pcm[:len(pcm) - len(pcm) % frame]
        if not pcm:
            return b""
        x = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels).astype(np.float64) / 32768.0
        if self._core is None and not self._want:
            self._remember(x)
            return pcm
        if self._core is None:
            self._start()
        core = self._core
        core.mix_target = 1.0 if self._want else 0.0
        self._pending = np.concatenate([self._pending, x])
        y = core.process(x)
        if self._discard:
            drop = min(self._discard, len(y))
            y = y[drop:]
            self._discard -= drop
        self._pending = self._pending[len(y):]
        self._remember(x)
        # Two whole frames at a gain of exactly 1: nothing still inside the
        # core was touched, so what it holds is the raw audio. Hand that
        # back as it is and stop.
        if not self._want and core.mix == 0.0 and core.frames_at_target >= 2:
            y = np.concatenate([y, self._pending])
            self._pending = np.zeros((0, self.channels))
            self._core = None
        return self._to_pcm(y)

    def _start(self) -> None:
        core = Nr2(self.rate, self.channels, self._level)
        core.mix = 0.0
        # The core's half-frame of history is the audio just passed through,
        # so its first frame overlaps what the listener has already heard.
        # The first hop it returns is that audio again (and only half
        # reconstructed), so it is dropped; everything after it is the
        # stream's next sample onwards.
        core._in = self._hist[-core.delay:].copy()
        self._discard = core.hop
        self._pending = np.zeros((0, self.channels))
        self._core = core

    def _remember(self, x: np.ndarray) -> None:
        n = len(self._hist)
        if len(x) >= n:
            self._hist = x[-n:].copy()
        else:
            self._hist = np.concatenate([self._hist[len(x):], x])

    def _to_pcm(self, y: np.ndarray) -> bytes:
        if not len(y):
            return b""
        return (np.clip(np.round(y * 32768.0), -32768, 32767).astype("<i2")).tobytes()
