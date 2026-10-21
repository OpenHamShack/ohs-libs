"""fldigi's CW receiver, ported line for line to Python for the decoder bench.

Source: fldigi 4.2.06 (W1HKJ and others, GPL-3.0-or-later), src/cw/cw.cxx
(cw::rx_FFTprocess, cw::decode_stream, cw::handle_event, cw::update_tracking,
cw::sync_parameters) and src/filters/fftfilt.cxx (create_filter). This file
is GPL like the rest of ohs-libs, and it is a measuring instrument for
tools/decoder_bench only: nothing in the shipped library imports it.

Why a port and not fldigi itself: fldigi decodes only from a sound card in
real time behind an FLTK window, and the bench has hours of audio. The
receive path is short and self-contained (mixer, one FFT low-pass, decimate
by 16, a moving average, peak/floor trackers, a hysteresis detector, and
the dot/dash timing state machine), so it is reproduced here with
fldigi's defaults (configuration.h): CWspeed 18, CWbandwidth 150 Hz (no
matched filter), CWtrack on, attack/decay medium, squelch on at 5
(status.cxx), SOM decoding off. The one liberty: the overlap-add FFT
filter is done as one linear convolution with the same 1024-tap
Blackman-windowed sinc, normalised the same way; the output is the same
apart from fldigi's block latency.

Durations are counted in 8 kHz samples as in fldigi (the source calls them
"usec"); a timestamp is kept beside them for scoring.
"""
from __future__ import annotations

import math

import numpy as np

from shack_cw.morse import CODE_TO_CHAR, CODE_TO_PROSIGN

SR = 8000
KWPM = 12 * SR // 10           # samples in a dot = KWPM / WPM
DEC_RATIO = 16
FFT_SIZE = 2048                # CW_FFT_SIZE: the filter has FFT_SIZE/2 taps
MAX_MORSE_ELEMENTS = 6
TRACKING_FILTER_SIZE = 16


def _lpf_taps(fc: float, flen: int = FFT_SIZE) -> np.ndarray:
    """fftfilt::create_filter(0, fc): a Blackman sinc of flen/2 taps,
    scaled so the peak of its response over the first flen/2 bins is 1."""
    n2 = flen // 2
    i = np.arange(n2)
    x = i - n2 // 2
    with np.errstate(invalid="ignore", divide="ignore"):
        h = np.where(x == 0, 2.0 * fc, np.sin(2 * np.pi * fc * x) / (np.pi * x))
    h = h * (0.42 - 0.5 * np.cos(2 * np.pi * i / n2) + 0.08 * np.cos(4 * np.pi * i / n2))
    H = np.fft.fft(h, flen)
    return h / np.max(np.abs(H[:n2]))


def _convolve(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """The causal FIR output, len(x) long, by overlap-add in numpy (the
    shack_cw test venv has no scipy)."""
    L, M = 1 << 15, len(h)
    nfft = 1 << int(np.ceil(np.log2(L + M - 1)))
    H = np.fft.fft(h, nfft)
    y = np.zeros(len(x) + nfft, np.complex128)
    for i in range(0, len(x), L):
        seg = np.fft.ifft(np.fft.fft(x[i:i + L], nfft) * H)
        y[i:i + nfft] += seg
    return y[:len(x)]


class _MovAvg:
    """filters.cxx Cmovavg: a running sum, primed with the first value."""

    def __init__(self, n: int):
        self.n, self.buf, self.p, self.out, self.empty = n, [0.0] * n, 0, 0.0, True

    def run(self, a: float) -> float:
        if self.empty:
            self.empty = False
            self.buf = [a] * self.n
            self.out = a * self.n
            self.p = 0
            return a
        self.out = self.out - self.buf[self.p] + a
        self.buf[self.p] = a
        self.p = (self.p + 1) % self.n
        return self.out / self.n


def _decayavg(avg: float, x: float, w: int) -> float:
    return x if w <= 1 else (x - avg) / w + avg


class FldigiCw:
    """One fldigi CW receiver at `freq` Hz of 8 kHz audio."""

    def __init__(self, freq: float = 700.0, speed: float = 18.0, bandwidth: float = 150.0,
                 squelch: bool = True, squelch_level: float = 5.0):
        self.freq = freq
        self.cw_speed = speed
        self.cw_send_speed = int(speed)
        self.taps = _lpf_taps(bandwidth / SR)
        symbollen = int(round(SR * 1.2 / speed))
        self.bitfilter = _MovAvg(max(1, symbollen // (2 * DEC_RATIO)))
        self.tracking = _MovAvg(TRACKING_FILTER_SIZE)
        self.sql_on, self.sql_level = squelch, squelch_level
        self.two_dots = int(2 * KWPM / speed)
        self.cw_send_dot_length = int(KWPM / speed)
        self.agc_peak = 0.0          # rx_init
        self.noise_floor = 1.0
        self.sig_avg = 0.0
        self.metric = 0.0
        self.state = "idle"
        self.smpl_ctr = 0
        self.start_ts = self.end_ts = 0
        self.rep = ""
        self.space_sent = True        # handle_event's statics
        self.last_element = 0
        self._sync()

    def _sync(self) -> None:
        # sync_parameters with CWtrack on and the speed setting unchanged.
        self.cw_receive_speed = int(KWPM / (self.two_dots // 2)) if self.two_dots >= 2 else 5
        self.dot = KWPM // self.cw_receive_speed if self.cw_receive_speed > 0 else KWPM // 5
        self.spike = self.dot // 2

    def _update_tracking(self, d1: int, d2: int) -> None:
        min_dot, max_dash = KWPM // 200, 3 * KWPM // 5
        if d1 > d2 and d1 > 4 * d2:
            return
        if d2 > d1 and d2 > 4 * d1:
            return
        if d1 < min_dot or d2 < min_dot:
            return
        if d2 > max_dash:
            return
        self.two_dots = int(self.tracking.run((d1 + d2) // 2))
        self._sync()

    def decode(self, audio: np.ndarray) -> list[tuple[float, str]]:
        """[(seconds, character)] for a whole clip of 8 kHz audio."""
        n = np.arange(len(audio))
        z = audio.astype(np.float64) * np.exp(1j * 2 * np.pi * self.freq * n / SR)
        y = _convolve(z, self.taps)
        mags = np.abs(y[DEC_RATIO - 1::DEC_RATIO])   # smpl_ctr % 16 == 0 after the increment
        out: list[tuple[float, str]] = []
        attack, decay = 200, 1000
        for k, m in enumerate(mags.tolist()):
            self.smpl_ctr += DEC_RATIO
            t = (k + 1) * DEC_RATIO / SR
            value = self.bitfilter.run(m)
            # decode_stream
            self.sig_avg = _decayavg(self.sig_avg, value, decay)
            if value < self.sig_avg:
                self.noise_floor = _decayavg(self.noise_floor, value, attack if value < self.noise_floor else decay)
            if value > self.sig_avg:
                self.agc_peak = _decayavg(self.agc_peak, value, attack if value > self.agc_peak else decay)
            norm_noise = self.noise_floor / self.agc_peak if self.agc_peak else 0.0
            norm_sig = self.sig_avg / self.agc_peak if self.agc_peak else 0.0
            v = value / self.agc_peak if self.agc_peak else 0.0
            self.metric *= 0.8
            if 1e-4 < self.noise_floor < self.sig_avg:
                self.metric += 0.2 * min(max(2.5 * 20 * math.log10(self.sig_avg / self.noise_floor), 0.0), 100.0)
            diff = norm_sig - norm_noise
            upper = norm_sig - 0.2 * diff
            lower = norm_noise + 0.7 * diff
            if not self.sql_on or self.metric > self.sql_level:
                if v > upper and self.state != "tone":
                    self._keydown()
                if v < lower and self.state == "tone":
                    self._keyup()
            ch = self._query()
            if ch is not None:
                out.append((t, ch))
        return out

    def _keydown(self) -> None:
        if self.state == "idle":
            self.smpl_ctr = 0
            self.rep = ""
        self.start_ts = self.smpl_ctr
        self.state = "tone"

    def _keyup(self) -> None:
        self.end_ts = self.smpl_ctr
        el = max(0, self.end_ts - self.start_ts)
        if self.spike > 0 and el < self.spike:
            self.state = "idle"
            return
        if self.last_element > 0:
            if 2 * self.last_element < el < 4 * self.last_element:
                self._update_tracking(self.last_element, el)
            if 2 * el < self.last_element < 4 * el:
                self._update_tracking(el, self.last_element)
        self.last_element = el
        self.rep += "." if el <= self.two_dots else "-"
        if len(self.rep) > MAX_MORSE_ELEMENTS:
            self.state = "idle"
            self.smpl_ctr = 0
            return
        self.state = "after"

    def _query(self) -> str | None:
        if self.state == "tone":
            return None
        el = max(0, self.smpl_ctr - self.end_ts)
        if el < 2 * self.dot:
            return None
        if el <= 4 * self.dot and self.state == "after":
            ch = CODE_TO_PROSIGN.get(self.rep) or CODE_TO_CHAR.get(self.rep) or "*"
            self.rep = ""
            self.state = "idle"
            self.space_sent = False
            return ch
        if el > 4 * self.dot and not self.space_sent:
            self.space_sent = True
            return " "
        return None
