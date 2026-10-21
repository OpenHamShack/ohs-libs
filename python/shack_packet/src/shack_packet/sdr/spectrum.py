"""Waterfall rows: the capture's spectrum, a few times a second, in dBFS.

Its own transform, not the channeliser's: the channeliser's FFT has no
window (overlap-save needs none), so a strong signal's sidelobes would
smear the whole display. Here each row is the average of `average` Hann-
windowed FFTs of `nfft` points, taken from the start of each row's time
(not every sample: a display needs the shape, and 8 x 4096 points ten
times a second is about 3 ms of CPU a second at 2.4 MS/s).

Rows are fftshifted (lowest frequency first) and `width` bins wide - each
display bin the strongest of the FFT bins under it, so a narrow signal
does not vanish between pixels - with `freqs()` giving each column's
frequency on the air.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import PacketError


@dataclass
class Row:
    time: float          # UTC of the row's first sample
    db: np.ndarray       # (width,) float32, dBFS per FFT bin's bandwidth


class Spectrum:
    def __init__(self, rate: float, centre: float, *, nfft: int = 4096, width: int = 1024,
                 rows_per_s: float = 10.0, average: int = 8):
        if nfft % width:
            raise PacketError("nfft must be a multiple of width")
        self.rate = float(rate)
        self.centre = float(centre)
        self.nfft = nfft
        self.width = width
        self.every = int(round(rate / rows_per_s))
        self.average = max(1, min(average, self.every // nfft))
        self.win = np.hanning(nfft).astype(np.float32)
        # A full-scale tone reads 0 dBFS: the window's coherent gain squared.
        self._norm = 1.0 / float(self.win.sum()) ** 2
        self._next = 0                 # IQ index where the next row starts
        self._have = np.zeros(0, np.complex64)
        self._have_at = 0

    def freqs(self) -> np.ndarray:
        """Each column's centre frequency on the air (Hz)."""
        f = (np.arange(self.nfft) - self.nfft // 2) * (self.rate / self.nfft) + self.centre
        return f.reshape(self.width, -1).mean(axis=1)

    def feed(self, z: np.ndarray, index: int, time0: float) -> list[Row]:
        """IQ block at `index` (its first sample's number), UTC `time0`."""
        rows = []
        need = self.average * self.nfft
        if index > self._have_at + len(self._have):          # a gap: what was gathered is stale
            self._have = np.zeros(0, np.complex64)
            self._have_at = index
            self._next = max(self._next, index)
        end = index + len(z)
        while self._next + need <= end:
            lo = self._next - index
            if lo >= 0:
                seg = z[lo:lo + need]
            else:
                seg = np.concatenate([self._have[len(self._have) + lo:], z[:lo + need]])
            x = seg.reshape(self.average, self.nfft) * self.win
            p = (np.abs(np.fft.fft(x, axis=1)) ** 2).mean(axis=0) * self._norm
            p = np.fft.fftshift(p).reshape(self.width, -1).max(axis=1)
            rows.append(Row(time0 + (self._next - index) / self.rate,
                            (10 * np.log10(p + 1e-20)).astype(np.float32)))
            self._next += self.every
        keep = min(len(z), need)
        self._have = np.asarray(z[-keep:], np.complex64)
        self._have_at = end - keep
        return rows
