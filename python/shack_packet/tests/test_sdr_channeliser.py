"""The channeliser: each channel's signal comes out of the capture at the
right frequency, with its phase whole across blocks, the neighbours shut
out, the crystal corrected, and gaps accounted for; and the plan it is
given is data."""

from __future__ import annotations

import numpy as np
import pytest

from shack_packet.sdr import plan as planmod
from shack_packet.sdr.channeliser import Channeliser, pick_decimation
from shack_packet.sdr.plan import Channel, ChannelPlan
from shack_packet.sdr.source import SdrError
from shack_packet.sdr.spectrum import Spectrum

RATE = 2_400_000
CENTRE = 145_000_000.0


def tone(hz_bb: float, n: int, amp: float = 0.1, start: int = 0) -> np.ndarray:
    t = (start + np.arange(n)) / RATE
    return (amp * np.exp(2j * np.pi * hz_bb * t)).astype(np.complex64)


def run(ch: Channeliser, z: np.ndarray, block: int = 120_000):
    outs = [ch.feed(z[i:i + block]) for i in range(0, len(z), block)]
    audio = np.concatenate([o.audio for o in outs], axis=1)
    power = np.concatenate([o.power for o in outs], axis=1)
    return audio, power


@pytest.mark.parametrize("rate,want", [(2_400_000, (125, 19_200)), (2_048_000, (100, 20_480)),
                                       (1_920_000, (100, 19_200)), (1_024_000, (50, 20_480)),
                                       (2_880_000, (150, 19_200)), (3_200_000, (160, 20_000))])
def test_the_channel_rate_is_a_whole_number_near_19k2(rate, want):
    assert pick_decimation(rate) == want


def test_a_rate_with_no_whole_channel_rate_is_refused_with_a_reason():
    with pytest.raises(SdrError) as e:
        pick_decimation(2_400_001)
    assert e.value.reason == "rate-unsupported"


def test_a_carrier_off_frequency_reads_as_that_offset_steadily_across_blocks():
    """The discriminator's output is the station's frequency error; a block
    phase left uncorrected would show as a spike every block."""
    ch = Channeliser(RATE, CENTRE, [Channel("a", 144_800_000), Channel("b", 145_212_500)])
    z = tone(-200_000 + 350.0, RATE) + tone(212_500 - 1_234.0, RATE)
    audio, _ = run(ch, z, block=77_777)              # blocks that do not line up with the FFT's
    settled = audio[:, ch.ch_rate // 2:] * ch.deviation_hz
    assert np.allclose(settled[0], 350.0, atol=0.5)
    assert np.allclose(settled[1], -1_234.0, atol=0.5)


def test_the_neighbouring_channel_is_60_db_down():
    ch = Channeliser(RATE, CENTRE, [Channel("mine", 144_925_000)])
    _, quiet = run(ch, tone(-75_000 + 12_500, RATE, amp=1.0))         # full scale, one channel up
    _, own = run(ch, tone(-75_000, RATE, amp=1.0))
    rejection = 10 * np.log10(own[0, 20:].mean() / quiet[0, 20:].mean())
    assert rejection > 60


def test_a_signal_inside_the_channel_passes_flat():
    ch = Channeliser(RATE, CENTRE, [Channel("mine", 144_925_000)])
    levels = []
    for off in (0.0, 3_000.0, -5_500.0):
        _, p = run(ch, tone(-75_000 + off, RATE // 2, amp=0.5))
        levels.append(10 * np.log10(p[0, 10:].mean()))
    assert max(levels) - min(levels) < 0.2
    assert abs(levels[0] - 10 * np.log10(0.25)) < 0.2       # dBFS of an amplitude-0.5 tone


def test_ppm_moves_every_channel_by_its_own_frequency():
    """A receiver 30 ppm fast hears 144.8 MHz 4344 Hz low; told so, the
    channel is where it should be."""
    ppm = 30.0
    f = 144_800_000
    seen = f / (1 + ppm * 1e-6) - CENTRE
    for told, want in ((0.0, seen - (f - CENTRE)), (ppm, 0.0)):
        ch = Channeliser(RATE, CENTRE, [Channel("a", f)], ppm=told)
        audio, _ = run(ch, tone(seen, RATE // 2))
        assert np.median(audio[0, 2000:]) * ch.deviation_hz == pytest.approx(want, abs=1.0)


def test_a_channel_outside_the_capture_is_refused():
    with pytest.raises(SdrError) as e:
        Channeliser(RATE, CENTRE, [Channel("far", 146_500_000)])
    assert e.value.reason == "out-of-range"


def test_a_gap_restarts_the_stream_and_audio_maps_back_to_the_right_iq_sample():
    ch = Channeliser(RATE, CENTRE, [Channel("a", 144_800_000)])
    ch.feed(np.zeros(240_000, np.complex64), index=0)
    n_before = ch.out_n
    ch.feed(np.zeros(240_000, np.complex64), index=1_000_000)       # 760 000 samples lost
    assert ch.iq_index(0) == -ch.V // 2
    assert ch.iq_index(n_before + 10) == 1_000_000 - ch.V // 2 + 10 * ch.D
    assert ch.iq_index(n_before - 1) == (n_before - 1) * ch.D - ch.V // 2


def test_the_default_plan_is_the_file_and_fits_one_rtl_sdr():
    plan = planmod.default_plan()
    assert [c.hz for c in plan] == [144_800_000, 144_925_000, 144_937_500, 144_950_000, 145_825_000]
    assert {c.kind for c in plan} <= set(planmod.KINDS)
    centre = plan.centre_for(2_400_000)
    assert centre is not None
    assert all(abs(c.hz - centre) >= 25_000 and abs(c.hz - centre) < 0.4 * 2_400_000 for c in plan)
    Channeliser(2_400_000, centre, list(plan))            # and the channeliser takes it
    assert plan.centre_for(1_024_000) is None              # 1 MHz of channels does not fit 1 MS/s


def test_a_plan_comes_from_the_band_plan_datasets_spots():
    p = planmod.from_spots([{"hz": 144800000, "label": "APRS", "mode": "fm"},
                            {"hz": 144_675_000, "label": "Data", "mode": "fm"}])
    assert [(c.hz, c.kind) for c in p] == [(144_800_000, "aprs"), (144_675_000, "other")]
    again = planmod.from_dict(p.to_dict())
    assert again == p


def test_two_channels_with_one_id_are_refused():
    with pytest.raises(ValueError):
        ChannelPlan((Channel("x", 1.0), Channel("x", 2.0)))


def test_spectrum_rows_put_a_tone_in_its_column_at_its_level():
    sp = Spectrum(RATE, CENTRE, nfft=4096, width=1024, rows_per_s=10, average=4)
    z = tone(300_000.0, RATE, amp=1.0)
    rows = []
    for i in range(0, RATE, 100_000):
        rows += sp.feed(z[i:i + 100_000], i, 1000.0 + i / RATE)
    assert len(rows) == 10
    assert rows[3].time == pytest.approx(1000.3)
    col = int(np.argmax(rows[5].db))
    assert sp.freqs()[col] == pytest.approx(CENTRE + 300_000, abs=RATE / 1024)
    assert rows[5].db[col] == pytest.approx(0.0, abs=1.5)        # 0 dBFS: a full-scale tone
    assert np.median(rows[5].db) < -100
