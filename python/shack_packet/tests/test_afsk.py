"""The Bell 202 modulator: the right tones, no phase jumps, exact bit timing
at every sample rate, and a transmission laid out as asked."""

from __future__ import annotations

import numpy as np
import pytest
from helpers import frame_bytes

from shack_packet import afsk, fx25, hdlc

RATES = [8000, 12_000, 16_000, 22_050, 24_000, 44_100, 48_000]


def peak_hz(x, rate):
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1.0 / rate)[np.argmax(spectrum)]


@pytest.mark.parametrize("rate", RATES)
def test_mark_and_space_are_1200_and_2200_hz(rate):
    mark = afsk.Modulator(rate).levels([0] * 1200)
    space = afsk.Modulator(rate).levels([1] * 1200)
    resolution = 1.0                                   # one second of each
    assert abs(peak_hz(mark, rate) - 1200) <= resolution
    assert abs(peak_hz(space, rate) - 2200) <= resolution


@pytest.mark.parametrize("rate", RATES)
def test_the_phase_never_jumps(rate):
    """At a tone change the waveform carries on from where it was: the
    largest step between samples is never more than the faster tone's."""
    rng = np.random.default_rng(rate)
    mod = afsk.Modulator(rate, amplitude=0.8)
    x = np.concatenate([mod.levels(rng.integers(0, 2, 37)) for _ in range(20)])
    limit = 0.8 * 2 * np.pi * 2200 / rate * 1.0001
    assert np.max(np.abs(np.diff(x))) <= limit
    assert np.max(np.abs(x)) <= 0.8 + 1e-9


@pytest.mark.parametrize("rate", RATES)
def test_bits_take_exactly_their_time_however_the_stream_is_cut(rate):
    rng = np.random.default_rng(1)
    levels = rng.integers(0, 2, 12_345)
    whole = afsk.Modulator(rate).levels(levels)
    # 12,345 bits at 1200 baud, to the sample.
    assert abs(len(whole) - 12_345 * rate / 1200) < 1.0
    mod = afsk.Modulator(rate)
    pieces, at = [], 0
    while at < len(levels):
        k = int(rng.integers(1, 300))
        pieces.append(mod.levels(levels[at:at + k]))
        at += k
    cut = np.concatenate(pieces)
    assert len(cut) == len(whole)
    assert np.max(np.abs(cut - whole)) < 1e-6


def test_nrzi_is_carried_between_calls():
    frame = frame_bytes("GM0ABC", "APDW17", ">x")
    bits = hdlc.frame_bits(frame, 4, 2)
    whole = afsk.Modulator(12_000).bits(bits)
    mod = afsk.Modulator(12_000)
    cut = np.concatenate([mod.bits(bits[:50]), mod.bits(bits[50:])])
    assert np.max(np.abs(cut - whole)) < 1e-9


def test_a_rate_that_cannot_carry_the_space_tone_is_refused():
    with pytest.raises(ValueError):
        afsk.Modulator(4000)


def test_txdelay_and_tail_are_flags():
    frame = frame_bytes("GM0ABC", "APDW17", ">x")
    bits = afsk.transmission_bits([frame], txdelay_ms=300, tail_ms=30)
    # 300 ms at 1200 baud is 360 bits: 45 flags; 30 ms is 36 bits: 5 flags.
    assert bits[:45 * 8] == list(hdlc.FLAG_BITS) * 45
    assert bits[-5 * 8:] == list(hdlc.FLAG_BITS) * 5
    assert bits[45 * 8:-5 * 8] == hdlc.frame_bits(frame, 0, 1)


def test_fx25_is_used_when_asked_and_plain_ax25_when_it_cannot_be():
    short = frame_bytes("GM0ABC", "APDW17", ">short")
    long_ = frame_bytes("GM0ABC", "APDW17", "", info_bytes=b"x" * 250)
    bits = afsk.transmission_bits([short, long_], fx25_mode="auto", txdelay_ms=0, tail_ms=0)
    coded = fx25.frame_bits(short)
    assert bits[8:8 + len(coded)] == coded
    rest = bits[8 + len(coded) + 8:-8]
    assert rest == hdlc.frame_bits(long_, 0, 1)


def test_pcm16_is_little_endian_and_clipped():
    raw = afsk.pcm16(np.array([0.0, 1.0, -1.0, 2.0, -0.5]))
    assert np.frombuffer(raw, "<i2").tolist() == [0, 32767, -32767, 32767, -16384]


def test_afsk1200_occupied_bandwidth_is_what_the_pluto_transmits():
    """Mode.occupied_hz for afsk1200 is the 99 % bandwidth of random frames
    through the Pluto's FM modulator (fsk_bench's measure), and it sits in
    a 12.5 kHz channel; the Link test page shows it."""
    import numpy as np
    from shack_packet import modem
    from shack_packet.radios.pluto import FmModulator
    m = modem.mode("afsk1200")
    rng = np.random.default_rng(3)
    frames = [bytes(rng.integers(0, 256, 200).astype(np.uint8)) for _ in range(30)]
    audio = np.asarray(modem.modulate(frames, 12_000, mode=m, txdelay_ms=10), dtype=float)
    iq = FmModulator(rate_iq=2_400_000, ifo_hz=0.0)(audio)
    n = 1 << 18
    power = np.zeros(n)
    for i in range(len(iq) // n):
        power += np.abs(np.fft.fft(iq[i * n:(i + 1) * n] * np.hanning(n))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / 2_400_000))
    power = np.fft.fftshift(power)
    c = np.cumsum(power) / power.sum()
    width = f[np.searchsorted(c, 0.995)] - f[np.searchsorted(c, 0.005)]
    assert abs(width - m.occupied_hz) < 0.05 * m.occupied_hz
    assert 10 * np.log10(power[np.abs(f) > 6_250].sum() / power.sum()) < -20
