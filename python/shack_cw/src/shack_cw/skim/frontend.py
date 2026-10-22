"""The skimmer's filter bank: one short-time FFT over the whole CW segment.

One transform serves every decoder at once (CW-SKIMMER-PLAN §1.4): a 5 ms
hop and a Hann window two hops long - exactly the decoder's own front end
(Decoder._feed) - zero-padded to bins of about 23 Hz. Each signal's decoder
is then driven from seven bins of it (the tone and REF_OFFSETS), which
costs a gather and a multiply per channel, not a mixer per channel. The
alternative, a bank channel per signal cut on the receive thread, was
cheap too but put births and deaths on the wrong thread (§1.1).

Detection needs to resolve stations 60-100 Hz apart, which a 10 ms window
cannot (its main lobe is +-200 Hz wide; the zero padding only
interpolates). So detection has its own transform beside it: a 40 ms Hann,
hopped every 20 ms, into the same bins - a quarter as many FFTs, and a
main lobe a quarter as wide. Its rows are power, for detect.py.

Everything here is numpy over the whole segment; there is no Python per
signal except the gather's bookkeeping.
"""
from __future__ import annotations

import numpy as np

from ..decoder import FRAME_S, REF_OFFSETS
from ..i18n import _

BIN_HZ = 23.4375      # the bins wanted: 96 kS/s / 4096, 12 kS/s / 512
DET_HOPS = 4          # the detection transform's hop, in frames (20 ms)
DET_WIN = 8           # its window, in frames (40 ms)


def _nfft(rate: float) -> int:
    """A power of two giving bins of about BIN_HZ at `rate`."""
    return 1 << int(round(np.log2(rate / BIN_HZ)))


class Stft:
    """Feed complex blocks of the segment; get back each block's decoder
    spectra (one row per 5 ms frame) and detection rows (one per 20 ms).

    Frames are numbered from the first sample (`frame0` of each result), so
    the gather can carry every frequency's phase across blocks."""

    def __init__(self, rate: float):
        self.rate = float(rate)
        hop = self.rate * FRAME_S
        if abs(hop - round(hop)) > 1e-9:
            raise ValueError(_("a 5 ms frame must be a whole number of samples at {rate} S/s").format(rate=rate))
        self.hop = int(round(hop))
        # Bins of about BIN_HZ, or finer where the 40 ms detection window
        # would not fit: Radio's 40 m segment comes at 60 kS/s, where the
        # nearest power of two (2048) is shorter than the window (2400).
        self.nfft = max(_nfft(self.rate), 1 << int(np.ceil(np.log2(DET_WIN * self.hop))))
        self.df = self.rate / self.nfft
        i = np.arange(2 * self.hop)
        # The decoder's window exactly (Decoder._feed): two frames, centred
        # on the frame, so each sample counts in two frames.
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * (i + 0.5) / (2 * self.hop))).astype(np.float32)
        j = np.arange(DET_WIN * self.hop)
        self.det_win = (0.5 - 0.5 * np.cos(2 * np.pi * (j + 0.5) / len(j))).astype(np.float32)
        # Noise bandwidth of one detection bin, for SNR in other bandwidths.
        self.det_enbw = float(self.rate * np.sum(self.det_win ** 2) / np.sum(self.det_win) ** 2)
        self.reset()

    def reset(self) -> None:
        # One frame of history before the first: the first window starts a
        # frame before its own frame, as the decoder's does.
        self._buf = np.zeros(self.hop, np.complex64)
        self.frames = 0                 # frames produced so far
        self._det_buf = np.zeros(0, np.complex64)
        self._det_skip = 0              # samples of the stream before _det_buf's first

    def freqs(self) -> np.ndarray:
        """Each bin's frequency from the stream's centre, Hz (fftshifted
        order is not used: bin k is k * df, negative above nfft/2)."""
        return np.fft.fftfreq(self.nfft, 1.0 / self.rate)

    def bin_of(self, hz) -> np.ndarray:
        """The nearest bin to each frequency (Hz from the centre)."""
        return np.rint(np.asarray(hz) / self.df).astype(np.int64) % self.nfft

    def feed(self, z: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
        """(frame0, X, P): the number of the first new frame, the new
        frames' complex spectra (frames, nfft), and the new detection rows'
        power (rows, nfft)."""
        z = np.asarray(z, np.complex64)
        buf = np.concatenate([self._buf, z])
        n = (len(buf) - self.hop) // self.hop        # whole new frames
        frame0 = self.frames
        if n <= 0:
            self._buf = buf
            return frame0, np.zeros((0, self.nfft), np.complex64), np.zeros((0, self.nfft), np.float32)
        x = np.lib.stride_tricks.sliding_window_view(buf, 2 * self.hop)[::self.hop][:n]
        X = np.fft.fft(x * self.win, self.nfft, axis=1).astype(np.complex64)
        self._buf = buf[n * self.hop:]
        self.frames += n
        # Detection: 40 ms windows every 20 ms, over the same samples.
        det = np.concatenate([self._det_buf, z])
        step, w = DET_HOPS * self.hop, DET_WIN * self.hop
        m = (len(det) - w) // step + 1 if len(det) >= w else 0
        if m > 0:
            y = np.lib.stride_tricks.sliding_window_view(det, w)[::step][:m]
            P = np.abs(np.fft.fft(y * self.det_win, self.nfft, axis=1)) ** 2
            self._det_buf = det[m * step:]
        else:
            P = np.zeros((0, self.nfft))
            self._det_buf = det
        return frame0, X, P.astype(np.float32)


class Gather:
    """Per-frame complex sums for a set of channels, coherent across
    frames, as Decoder._feed makes them for its one tone: each channel's
    seven frequencies (tone and REF_OFFSETS round its `f`) are read from
    their nearest bins - at most half a bin (11.7 Hz) off, which a 10 ms
    Hann loses about 0.03 dB to - and de-rotated by each frequency's own
    phase, carried on from frame to frame. Without that the coherent window
    (up to 8 frames) would turn round and cancel.

    The phase is per channel and kept here, so a channel whose frequency
    is nudged by the tracker turns on smoothly from where it was."""

    def __init__(self, stft: Stft, offsets=REF_OFFSETS):
        self.stft = stft
        self.off = np.array(offsets)
        self.phase: dict[int, np.ndarray] = {}

    def drop(self, key: int) -> None:
        self.phase.pop(key, None)

    def take(self, X: np.ndarray, keys: list[int], freqs: list[float]) -> np.ndarray:
        """(frames, len(keys), 7) complex sums for the channels `keys` at
        `freqs` (Hz from the stream's centre), from the frames X."""
        if not keys:
            return np.zeros((len(X), 0, len(self.off)), np.complex128)
        st = self.stft
        f = np.asarray(freqs, np.float64)[:, None] + self.off[None, :]          # (K, 7)
        bins = st.bin_of(f)
        w = 2 * np.pi * f * st.hop / st.rate                                     # per frame
        ph0 = np.array([self.phase.get(k, np.zeros(len(self.off))) for k in keys])
        n = len(X)
        ph = ph0[None, :, :] + w[None, :, :] * np.arange(n)[:, None, None]
        c = X[:, bins] * np.exp(-1j * ph)
        end = (ph0 + w * n) % (2 * np.pi)
        for i, k in enumerate(keys):
            self.phase[k] = end[i]
        return c
