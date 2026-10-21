"""NR2 as the apps use it: what it does to noise, to signals, and to a
stream that is cut into packets and switched on and off."""

from __future__ import annotations

import numpy as np
import pytest

from shack_dsp import Nr2, Nr2Stream, frame_for
from shack_dsp.bench import band_noise, cw, shadow, speech
from shack_dsp.nr2 import expint_e1


def test_exponential_integral_matches_tabulated_values():
    # Abramowitz and Stegun, table 5.1.
    got = expint_e1(np.array([0.01, 0.5, 1.0, 2.0, 10.0]))
    want = np.array([4.037929577, 0.559773595, 0.219383934, 0.048900511, 4.156968929e-6])
    assert np.allclose(got, want, rtol=2e-4)


@pytest.mark.parametrize("rate,frame", [(8000, 128), (12_000, 256), (24_000, 512), (48_000, 1024)])
def test_frame_is_about_21_ms_at_every_rate(rate, frame):
    assert frame_for(rate) == frame


def test_with_every_gain_one_the_output_is_the_input_delayed():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.1, 12_000)
    nr = Nr2(12_000)
    nr.mix = nr.mix_target = 0.0
    y = nr.process(x)
    d = nr.delay
    assert np.max(np.abs(y[d:] - x[:len(y) - d])) < 1e-12


def test_the_output_does_not_depend_on_how_the_input_is_cut():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.1, 36_000)
    whole = Nr2(12_000).process(x)
    nr = Nr2(12_000)
    parts, i = [], 0
    while i < len(x):
        k = int(rng.integers(1, 700))
        parts.append(nr.process(x[i:i + k]))
        i += k
    cut = np.concatenate(parts)
    assert len(cut) == len(whole)
    assert np.array_equal(cut, whole)


@pytest.mark.parametrize("rate", [8000, 12_000, 24_000, 48_000])
def test_the_noise_estimate_finds_stationary_noise_within_2_db(rate):
    rng = np.random.default_rng(2)
    nr = Nr2(rate)
    nr.process(rng.normal(0, 0.05, rate * 8))
    frames = rng.normal(0, 0.05, (2000, nr.n)) * nr.window
    true = np.mean(np.abs(np.fft.rfft(frames, axis=1)) ** 2, axis=0)
    err_db = 10 * np.log10(np.median(nr.noise[5:-5] / true[5:-5]))
    assert abs(err_db) < 2.0


def test_speech_in_noise_gains_at_least_8_db_at_level_50():
    rng = np.random.default_rng(3)
    clean = speech(rng, 10, 12_000)
    noise = band_noise(rng, len(clean), 12_000, 300, 2700)
    snr_in, snr_out, loss, _ = shadow(clean, noise, 6.0, 12_000, 50)
    assert snr_out - snr_in > 8.0
    assert loss > -2.0           # the voice is barely touched


def test_keyed_cw_in_noise_gains_at_least_7_db_and_keeps_its_level():
    rng = np.random.default_rng(4)
    clean = cw(rng, 10, 12_000)
    noise = band_noise(rng, len(clean), 12_000, 350, 850)
    snr_in, snr_out, loss, _ = shadow(clean, noise, 6.0, 12_000, 50)
    assert snr_out - snr_in > 7.0
    assert loss > -3.5


def test_a_higher_level_takes_more_noise_away():
    rng = np.random.default_rng(5)
    clean = speech(rng, 8, 12_000)
    noise = band_noise(rng, len(clean), 12_000, 300, 2700)
    removed = [shadow(clean, noise, 6.0, 12_000, level)[3] for level in (0, 50, 100)]
    assert removed[0] > removed[1] > removed[2]


def test_stereo_keeps_its_image():
    """One gain per bin for both channels: a signal on the left only stays
    on the left, and the right is only ever the right's own noise, scaled."""
    rng = np.random.default_rng(6)
    n = 24_000 * 4
    left = speech(rng, 4, 24_000)[:n] * 0.2 + band_noise(rng, n, 24_000, 300, 2700) * 0.02
    right = np.zeros(n)
    y = Nr2(24_000, channels=2).process(np.stack([left, right], axis=1))
    assert np.max(np.abs(y[:, 1])) == 0.0
    assert np.sqrt(np.mean(y[:, 0] ** 2)) > 0.05


# -- the stream the apps use ----------------------------------------------------

def _pcm(x):
    return np.clip(np.round(x * 32767), -32768, 32767).astype("<i2").tobytes()


def _samples(b):
    return np.frombuffer(b, dtype="<i2").astype(np.float64)


def test_off_is_the_input_untouched_and_undelayed():
    rng = np.random.default_rng(7)
    st = Nr2Stream(12_000)
    pcm = _pcm(rng.normal(0, 0.1, 480))
    assert st.feed(pcm) == pcm
    assert not st.active


def test_switching_on_and_off_loses_no_sample_and_makes_no_click():
    """A 1 kHz tone in light noise through 40-600 sample packets, switched
    every 0.4 s. Every sample comes out once, in order, and the largest
    sample-to-sample step is the tone's own: a click from a gain jump or a
    sample lost or repeated at a switch would be a step several times it."""
    rate = 12_000
    rng = np.random.default_rng(8)
    n = 4 * rate
    tone = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(n) / rate) + 0.01 * rng.normal(size=n)
    pcm = np.frombuffer(_pcm(tone), dtype="<i2")
    st = Nr2Stream(rate, level=100)
    out, i, toggle = [], 0, int(0.4 * rate)
    while i < n:
        if i >= toggle:
            st.on = not st.on
            toggle += int(0.4 * rate)
        k = int(rng.integers(40, 600))
        out.append(st.feed(pcm[i:i + k].tobytes()))
        i += k
    st.on = False
    tail = np.zeros(2400, dtype="<i2")
    for j in range(0, len(tail), 240):
        out.append(st.feed(tail[j:j + 240].tobytes()))
    y = _samples(b"".join(out))
    assert len(y) == n + len(tail)
    assert not st.active
    step_in = np.max(np.abs(np.diff(pcm.astype(np.float64))))
    assert np.max(np.abs(np.diff(y))) < 1.2 * step_in


def test_on_reduces_noise_in_the_stream():
    rate = 12_000
    rng = np.random.default_rng(9)
    noise = band_noise(rng, 5 * rate, rate, 300, 2700) * 0.05
    st = Nr2Stream(rate, level=50, on=True)
    out = b"".join(st.feed(_pcm(noise[i:i + 240])) for i in range(0, len(noise), 240))
    y = _samples(out)[3 * rate:] / 32767
    assert 10 * np.log10(np.mean(y ** 2) / np.mean(noise[3 * rate:] ** 2)) < -10.0


def test_the_level_can_change_while_it_runs():
    st = Nr2Stream(12_000, level=20, on=True)
    st.feed(_pcm(np.zeros(480)))
    st.level = 150
    assert st.level == 100.0
    st.level = -3
    assert st.level == 0.0


def test_an_odd_byte_is_dropped_not_misread():
    st = Nr2Stream(12_000, channels=2)
    assert len(st.feed(b"\x01\x00\x02\x00\x03")) == 4
