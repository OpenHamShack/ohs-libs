"""The receiver the bench hears through: QO-100 NB's CW path, copied exactly.

The bench measures the decoder on audio as a receiver hands it over, not
on clean tones: a 500 Hz CW filter round the pitch (a complex one-sided
Kaiser bandpass, 70 dB down, run by FFT overlap-save) and the medium AGC
(peak held 0.25 s, then 20 dB/s down). That is openhamshack-nb's
ohscore.dsp.demod.Demod in "cw" mode with ohscore.dsp.fir and ohscore.dsp.agc
under it, at main 47be138, cut down to what CW uses - the numbers the bench
printed in NB are the numbers it prints here only if this is the same
arithmetic, so it is a copy, not a re-design. openhamshack-nb's tools/cw_bench.py
runs the same cases through NB's own Demod and must print the same table.

A radio's own filter and AGC (an IC-9700's, a Flex's) are not this one, but
they are the same kind of thing; the decoder learns its noise floor beside
the tone and through whatever filter is there (Decoder._frame), which is
why one bench stands for them all.

Not part of the decoder's API: apps bring their own receiver.
"""
from __future__ import annotations

import math

import numpy as np


def _kaiser_beta(atten_db: float) -> float:
    if atten_db > 50:
        return 0.1102 * (atten_db - 8.7)
    if atten_db >= 21:
        return 0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    return 0.0


def _kaiser_taps(transition_hz: float, fs: float, atten_db: float = 80.0) -> int:
    n = (atten_db - 7.95) / (14.36 * transition_hz / fs) + 1
    return int(math.ceil(n)) | 1


def _lowpass(cutoff_hz: float, fs: float, ntaps: int, atten_db: float = 80.0) -> np.ndarray:
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = 2 * cutoff_hz / fs * np.sinc(2 * cutoff_hz / fs * n)
    h *= np.kaiser(ntaps, _kaiser_beta(atten_db))
    return h / h.sum()


def _bandpass_complex(lo_hz: float, hi_hz: float, fs: float, ntaps: int, atten_db: float = 80.0) -> np.ndarray:
    centre = (lo_hz + hi_hz) / 2.0
    h = _lowpass((hi_hz - lo_hz) / 2.0, fs, ntaps, atten_db)
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    return (h * np.exp(2j * np.pi * centre / fs * n)).astype(np.complex128)


def _good_size(n: int) -> int:
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return best


class _OlsFilter:
    def __init__(self, taps: np.ndarray):
        taps = np.asarray(taps)
        self.p = len(taps)
        self.nfft = _good_size(4 * self.p)
        self.step = self.nfft - (self.p - 1)
        self.H = np.fft.fft(taps, self.nfft).astype(np.complex64)
        self.hist = np.zeros(self.p - 1, np.complex64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.complex64)
        n = len(x)
        if n == 0:
            return np.zeros(0, np.complex64)
        buf = np.concatenate([self.hist, x])
        out = np.empty(n, np.complex64)
        nblk = -(-n // self.step)
        need = (nblk - 1) * self.step + self.nfft
        if len(buf) < need:
            buf = np.concatenate([buf, np.zeros(need - len(buf), np.complex64)])
        frames = np.lib.stride_tricks.sliding_window_view(buf, self.nfft)[::self.step][:nblk]
        y = np.fft.ifft(np.fft.fft(frames, axis=1) * self.H, axis=1)[:, self.p - 1:]
        out[:] = y.reshape(-1)[:n]
        if self.p > 1:
            self.hist = np.concatenate([self.hist, x])[-(self.p - 1):]
        return out


class _Agc:
    """NB's PeakGain with the Agc preset "medium": instant attack, the peak
    held 0.25 s, then 20 dB/s down, aiming at 0.3 of full scale."""

    def __init__(self, rate: float, hang: float = 0.25, decay_db_s: float = 20.0, chunk: int = 30):
        self.chunk = chunk
        self.target_db = 20 * np.log10(0.3)
        self.max_gain_db, self.min_gain_db = 100.0, -40.0
        self.h = max(0, int(round(hang * rate / chunk)))
        self.delta = decay_db_s * chunk / rate
        self.tail = np.full(self.h, -200.0)
        self.C = -np.inf
        self.last_gain_db = 0.0

    def _gains_db(self, mag: np.ndarray) -> np.ndarray:
        n = len(mag)
        starts = np.arange(0, n, self.chunk)
        p = 20 * np.log10(np.maximum.reduceat(mag, starts) + 1e-12)
        h, d = self.h, self.delta
        P = np.concatenate([self.tail, p])
        t0 = len(self.tail)
        i = np.arange(t0, len(P))
        padded = np.concatenate([np.full(h, -np.inf), P])
        A = np.lib.stride_tricks.sliding_window_view(padded, h + 1).max(axis=1)[t0:]
        q = np.maximum.accumulate(np.concatenate([[self.C], P + np.arange(len(P)) * d]))
        jmax = i - h - 1
        B = q[np.clip(jmax + 1, 0, None)] - (i - h) * d
        env = np.maximum(A, B)
        g = np.clip(self.target_db - env, self.min_gain_db, self.max_gain_db)
        s = len(P) - h
        if s > 0:
            older = (P[:s] + np.arange(s) * d).max()
            self.C = max(self.C, older) - s * d
        self.tail = P[s:] if h else np.zeros(0)
        return g

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        g = self._gains_db(np.abs(x))
        step = np.repeat(g, self.chunk)[:n]
        centres = np.arange(len(g)) * self.chunk + self.chunk / 2
        ramp = np.interp(np.arange(n), np.concatenate([[-self.chunk / 2], centres]),
                         np.concatenate([[self.last_gain_db], g]))
        gd = np.minimum(step, ramp)
        self.last_gain_db = float(g[-1])
        return (x * 10 ** (gd / 20)).astype(x.dtype)


class CwReceiver:
    """Complex baseband (the dial at 0 Hz) in, CW audio out: NB's Demod
    configured ("cw", pitch - 250, pitch + 250, "medium")."""

    def __init__(self, rate: float, pitch: float, width: float = 500.0):
        lo, hi = pitch - width / 2, pitch + width / 2
        trans = min(200.0, max(40.0, (hi - lo) / 8))
        n = min(_kaiser_taps(trans, rate, 70.0), 2047)
        self.filter = _OlsFilter(_bandpass_complex(lo, hi, rate, n, 70.0))
        self.agc = _Agc(rate)

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        a = self.filter(iq).real.astype(np.float32)
        return self.agc(a).astype(np.float32)
