"""The slip finder: known repeats and skips put into real AFSK, through a
radio-shaped channel (band-limited, delayed, quieter, noisy), come out
where they were put; a clean path comes out clean."""

from __future__ import annotations

import numpy as np
import pytest
from helpers import frame_bytes

from shack_packet import afsk, slips


def _over(rate: int) -> np.ndarray:
    frames = [frame_bytes("GM4BRI-1", "APZOHS", f"slip test {i}/3 the quick brown fox") for i in range(3)]
    return afsk.modulate(frames, rate=rate, amplitude=0.5, txdelay_ms=300)


def _radio(audio: np.ndarray, rate: int, delay: int, seed: int = 0) -> np.ndarray:
    """What a receiver hears: 300-3000 Hz, `delay` samples late, at a
    third of the level, under noise 30 dB down."""
    spectrum = np.fft.rfft(audio)
    f = np.fft.rfftfreq(len(audio), 1.0 / rate)
    spectrum[(f < 300) | (f > 3000)] = 0
    heard = np.fft.irfft(spectrum, len(audio)) * 0.3
    heard = np.concatenate([np.zeros(delay), heard, np.zeros(rate // 10)])
    return heard + np.random.default_rng(seed).normal(0, 0.3 * 0.5 * 0.03, len(heard))


@pytest.mark.parametrize("rate", [48_000, 12_000])
def test_a_clean_path_has_no_slips_and_its_delay_is_found(rate):
    sent = _over(rate)
    report = slips.find_slips(sent, _radio(sent, rate, delay=rate // 7), rate)
    assert report.slips == []
    assert report.drift_samples == 0
    assert abs(report.delay_samples - rate // 7) <= 1
    assert report.match > 0.9


def test_the_9700s_repeats_and_a_skipped_packet_are_found_where_they_were_put():
    rate = 48_000
    sent = _over(rate)
    put = [(0.45, 16), (0.62, 16), (0.81, -48), (1.02, 16)]
    report = slips.find_slips(sent, _radio(slips.apply_slips(sent, put, rate), rate, delay=3000), rate)
    assert [s.samples for s in report.slips] == pytest.approx([n for _, n in put], abs=2)
    assert [s.at_s for s in report.slips] == pytest.approx([t for t, _ in put], abs=0.015)
    assert report.drift_samples == pytest.approx(sum(n for _, n in put), abs=3)


def test_apply_slips_repeats_and_skips_exactly():
    audio = np.arange(100, dtype=float)
    out = slips.apply_slips(audio, [(0.010, 3), (0.050, -5)], 1000)
    assert out[:13].tolist() == [*range(10), 7, 8, 9]
    assert out[13:53].tolist() == list(range(10, 50))
    assert out[53:].tolist() == list(range(55, 100))


def test_the_command_line_reads_two_wav_files_and_reports(tmp_path, capsys):
    from shack_packet import bench
    rate = 48_000
    sent = _over(rate)
    bench.write_wav(tmp_path / "sent.wav", sent, rate)
    bench.write_wav(tmp_path / "heard.wav", _radio(slips.apply_slips(sent, [(0.6, 16)], rate), rate, 500), rate)
    assert slips.main([str(tmp_path / "sent.wav"), str(tmp_path / "heard.wav")]) == 0
    assert capsys.readouterr().out.startswith("1 slips in ")
