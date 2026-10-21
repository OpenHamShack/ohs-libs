"""The receiver: frames out of audio at every rate, whatever the cutting,
the level, the twist or the sender's clock; nothing invented out of noise;
each frame once; FX.25 preferred when it was sent."""

from __future__ import annotations

import numpy as np
import pytest
from helpers import frame_bytes

from shack_packet import afsk, ax25, bench, demod
from shack_packet.ax25 import Control, Frame, FrameType

RATES = [8000, 11_025, 12_000, 16_000, 22_050, 24_000, 44_100, 48_000]

FRAMES = [
    frame_bytes("GM0ABC-9", "APDW17", "!5537.14N/00420.83W>one", via=["WIDE1-1", "WIDE2-1"]),
    frame_bytes("MM0XYZ", "APDW17", ">status line"),
    Frame.build("G0ABC", "GB7BBS", Control(FrameType.SABM, pf=True)).encode(),
    Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, False, 3, 5), bytes(range(256))).encode(),
]


def decode(audio, rate, **kw):
    rx = demod.Receiver(rate, **kw)
    return rx.feed(audio) + rx.flush()


def frames_of(heard):
    return [h.frame for h in heard]


@pytest.mark.parametrize("rate", RATES)
def test_frames_come_back_at_every_rate(rate):
    audio = afsk.modulate(FRAMES, rate)
    assert frames_of(decode(audio, rate)) == FRAMES


@pytest.mark.parametrize("mode", [16, 32, 64, "auto"])
def test_fx25_frames_come_back_reported_as_fx25(mode):
    short = FRAMES[:3]
    heard = decode(afsk.modulate(short, 12_000, fx25_mode=mode), 12_000)
    assert frames_of(heard) == short
    assert all(h.fx25 is not None and h.corrected == 0 for h in heard)


def test_a_frame_too_long_for_fx25_still_arrives_as_plain_ax25():
    heard = decode(afsk.modulate(FRAMES, 12_000, fx25_mode=16), 12_000)
    assert frames_of(heard) == FRAMES
    assert [h.fx25 is None for h in heard] == [False, False, False, True]


def test_the_result_does_not_depend_on_how_the_audio_is_cut():
    clean = afsk.modulate(FRAMES[:2], 12_000)
    audio = afsk.pcm16(clean + np.random.default_rng(3).normal(0, 0.05, len(clean)))
    whole = [(h.frame, h.sample) for h in decode(audio, 12_000)]
    assert [f for f, _ in whole] == FRAMES[:2]
    rng = np.random.default_rng(4)
    for sizes in ("random", 1):
        rx = demod.Receiver(12_000)
        got, at = [], 0
        while at < len(audio):
            k = 2 * (int(rng.integers(1, 900)) if sizes == "random" else 1)
            got += rx.feed(audio[at:at + k])
            at += k
        got += rx.flush()
        assert [(h.frame, h.sample) for h in got] == whole


def test_the_reported_sample_is_where_the_frame_started():
    rate = 12_000
    lead = rate // 2
    audio = np.concatenate([np.zeros(lead), afsk.modulate([FRAMES[1]], rate, txdelay_ms=300)])
    heard = decode(audio, rate)
    # The frame's first bit follows 300 ms of flags (45, i.e. 360 bits).
    want = lead + 45 * 8 * rate / 1200
    assert abs(heard[0].sample - want) < 4 * rate / 1200


@pytest.mark.parametrize("amplitude", [0.005, 0.05, 0.5, 0.99])
def test_quiet_and_loud_signals_both_decode(amplitude):
    assert frames_of(decode(afsk.modulate(FRAMES, 12_000, amplitude=amplitude), 12_000)) == FRAMES


@pytest.mark.parametrize("twist", [-12, -9, -6, 6, 9, 12])
def test_twist_one_tone_far_weaker_than_the_other(twist):
    audio = bench.tilt(afsk.modulate(FRAMES, 12_000), 12_000, twist)
    assert frames_of(decode(audio, 12_000)) == FRAMES


def test_inverted_audio_decodes_the_same():
    audio = afsk.modulate(FRAMES, 12_000)
    assert frames_of(decode(-audio, 12_000)) == FRAMES


def test_hum_and_a_dc_offset_do_not_stop_it():
    rate = 12_000
    audio = afsk.modulate(FRAMES, rate, amplitude=0.3)
    t = np.arange(len(audio)) / rate
    audio = audio + 0.2 + 0.3 * np.sin(2 * np.pi * 50 * t) + 0.1 * np.sin(2 * np.pi * 88.5 * t)
    assert frames_of(decode(audio, rate)) == FRAMES


@pytest.mark.parametrize("error", [-0.02, -0.01, 0.01, 0.02])
def test_a_sender_whose_clock_is_two_percent_out(error):
    audio, frames = bench.synthetic(20, 8.0, 12_000, 0, seed=7, baud_error=error)
    assert set(frames_of(decode(audio, 12_000))) == set(frames)


def test_noise_decodes_nothing_rather_than_something_wrong():
    """The failure that matters: a plausible wrong frame would put a station
    in the wrong place with nothing to say so. Two minutes of noise at three
    levels, through every slicer and the FX.25 tag search."""
    rng = np.random.default_rng(11)
    for level in (0.01, 0.1, 0.7):
        rx = demod.Receiver(12_000)
        assert rx.feed(rng.normal(0, level, 12_000 * 40)) + rx.flush() == []


def test_in_noise_every_frame_out_is_one_that_was_sent_and_comes_out_once():
    audio, frames = bench.synthetic(40, 3.0, 12_000, 0, seed=3)
    heard = frames_of(decode(audio, 12_000))
    assert len(heard) == len(set(heard)), "a frame came out twice"
    assert set(heard) <= set(frames)
    assert len(heard) >= 20


def test_the_same_frame_sent_twice_comes_out_twice():
    one = FRAMES[1]
    back_to_back = afsk.modulate([one, one], 12_000)
    assert frames_of(decode(back_to_back, 12_000)) == [one, one]
    apart = np.concatenate([afsk.modulate([one], 12_000), np.zeros(6000), afsk.modulate([one], 12_000)])
    assert frames_of(decode(apart, 12_000)) == [one, one]


def test_fx25_gets_frames_through_noise_plain_ax25_does_not():
    """What FX.25 is for, measured: at 2 dB (in 6 kHz) plain AX.25 loses
    most frames and FX.25 with 16 check bytes almost none."""
    plain, frames = bench.synthetic(40, 2.0, 12_000, 0, seed=2)
    coded, _ = bench.synthetic(40, 2.0, 12_000, 0, seed=2, fx25_mode=16)
    got_plain = set(frames_of(decode(plain, 12_000))) & set(frames)
    heard = decode(coded, 12_000)
    got_coded = set(frames_of(heard)) & set(frames)
    assert len(got_plain) <= 15
    assert len(got_coded) >= 35
    assert sum(h.corrected for h in heard) > 0


def test_one_demodulator_alone_also_works():
    assert frames_of(decode(afsk.modulate(FRAMES, 12_000), 12_000, diversity=False)) == FRAMES


def test_what_comes_out_parses():
    for heard in decode(afsk.modulate(FRAMES, 48_000), 48_000):
        assert ax25.parse(heard.frame) is not None


def test_a_rate_that_cannot_carry_the_space_tone_is_refused():
    with pytest.raises(ValueError):
        demod.Receiver(5000)


def _dcd_trace(audio, rate, block_ms=20):
    rx = demod.Receiver(rate, block_ms=block_ms)
    step = int(rate * block_ms / 1000)
    trace = []
    for at in range(0, len(audio), step):
        rx.feed(audio[at:at + step])
        trace.append(rx.dcd)
    return np.array(trace)


@pytest.mark.parametrize("snr_db", [4, 10])
def test_carrier_detect_rises_inside_txdelay_and_falls_after(snr_db):
    """What CSMA stands on: DCD up before the preamble ends (so another
    station holds off before the first frame starts), down again once the
    channel is noise."""
    rate = 12_000
    rng = np.random.default_rng(snr_db)
    signal = afsk.modulate(FRAMES[:2], rate, txdelay_ms=300)
    sigma = np.sqrt(np.mean(signal ** 2) / 10 ** (snr_db / 10))
    audio = np.concatenate([np.zeros(rate), signal, np.zeros(rate)]) + rng.normal(0, sigma, 2 * rate + len(signal))
    trace = _dcd_trace(audio, rate)
    blocks_per_s = 50
    assert not trace[:blocks_per_s].any(), "carrier on noise before the transmission"
    first = int(np.flatnonzero(trace)[0]) - blocks_per_s
    assert first * 20 <= 300, f"carrier {first * 20} ms in; TXDELAY is 300"
    end = blocks_per_s + int(len(signal) / rate * blocks_per_s)
    assert trace[blocks_per_s + 15:end - 2].mean() > 0.95
    assert not trace[end + 10:].any(), "carrier stayed up on noise"


def test_no_carrier_on_noise_or_silence():
    rate = 12_000
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.3, rate * 30)
    assert not _dcd_trace(noise, rate).any()
    assert not _dcd_trace(np.convolve(noise, np.ones(4) / 4, "same"), rate).any()
    assert not _dcd_trace(np.zeros(rate * 2), rate).any()
