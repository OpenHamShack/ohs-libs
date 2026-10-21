"""The FLAC encoder: lossless, valid by the specification (tests/flacref.py)
and, where ffmpeg is installed, by a decoder somebody else wrote."""
from __future__ import annotations

import io
import shutil
import subprocess

import numpy as np
import pytest

from flacref import decode
from shack_rec.flac import BLOCK, FlacWriter


def _radio_audio(n: int, rate: int, channels: int = 1, seed: int = 0) -> np.ndarray:
    """Band-limited noise with a tone and a quiet stretch: what a receiver
    plays, loud enough to use most of the 16 bits in places."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / rate
    noise = np.convolve(rng.normal(0, 3000, n), np.ones(4) / 4, "same")
    tone = 12000 * np.sin(2 * np.pi * 700 * t)
    x = noise + tone
    x[n // 3:n // 3 + 2000] = 0                          # a muted stretch: CONSTANT subframes
    x = np.clip(np.round(x), -32768, 32767).astype(np.int16)
    if channels == 1:
        return x
    other = np.clip(np.round(x * 0.5 + rng.normal(0, 50, n)), -32768, 32767).astype(np.int16)
    return np.stack([x, other], axis=1)


def _encode(x: np.ndarray, rate: int, channels: int, chunk: int = 240, tags=None) -> bytes:
    f = io.BytesIO()
    w = FlacWriter(f, rate, channels, tags)
    for i in range(0, len(x), chunk):
        w.write(x[i:i + chunk])
    w.close()
    return f.getvalue()


@pytest.mark.parametrize("rate,channels", [(12_000, 1), (24_000, 2), (8_000, 1), (11_025, 1), (48_000, 1)])
def test_what_is_written_decodes_to_exactly_the_samples(rate, channels):
    x = _radio_audio(BLOCK * 3 + 777, rate, channels)
    got = decode(_encode(x, rate, channels))
    assert got["rate"] == rate and got["channels"] == channels and got["bps"] == 16
    assert got["total"] == len(x)
    assert np.array_equal(got["samples"], x.reshape(len(x), channels))


def test_full_scale_and_edge_values_survive():
    x = np.array([32767, -32768] * 3000 + [0, 1, -1] * 1000, np.int16)
    got = decode(_encode(x, 12_000, 1))
    assert np.array_equal(got["samples"][:, 0], x)


def test_silence_costs_almost_nothing_and_receive_audio_half_of_wav():
    silent = _encode(np.zeros(12_000 * 10, np.int16), 12_000, 1)
    assert len(silent) < 2000                            # ten seconds
    # An SSB receiver's noise: 300-2700 Hz, about -30 dBFS. LPC is what
    # gets this under half; fixed predictors alone left it at 74 %.
    rng = np.random.default_rng(1)
    spec = np.fft.rfft(rng.normal(0, 1, 12_000 * 10))
    f = np.fft.rfftfreq(12_000 * 10, 1 / 12_000)
    spec[(f < 300) | (f > 2700)] = 0
    x = np.fft.irfft(spec)
    x = np.round(x / x.std() * 1000).astype(np.int16)
    assert len(_encode(x, 12_000, 1)) < 0.5 * x.nbytes


def test_tags_carry_the_recordings_facts():
    tags = {"title": "144.300 MHz USB", "date": "2026-10-12T10:00:00Z", "frequency": 144300000, "mode": "USB",
            "station": "GM4BRI", "empty": ""}
    got = decode(_encode(np.zeros(100, np.int16), 12_000, 1, tags=tags))
    assert got["tags"] == {"TITLE": "144.300 MHz USB", "DATE": "2026-10-12T10:00:00Z", "FREQUENCY": "144300000",
                           "MODE": "USB", "STATION": "GM4BRI"}


def test_a_file_cut_off_before_close_still_decodes_up_to_the_cut():
    # A crash leaves STREAMINFO saying "length unknown" and the frames
    # written so far; nothing about that is invalid.
    x = _radio_audio(BLOCK * 4, 12_000)
    f = io.BytesIO()
    w = FlacWriter(f, 12_000, 1)
    w.write(x)
    got = decode(f.getvalue())                           # never closed
    assert got["total"] == 0
    assert np.array_equal(got["samples"][:, 0], x)


def test_writing_in_any_sized_pieces_gives_the_same_file():
    x = _radio_audio(BLOCK * 2 + 5, 12_000)
    assert _encode(x, 12_000, 1, chunk=1) == _encode(x, 12_000, 1, chunk=BLOCK * 5)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
@pytest.mark.parametrize("rate,channels", [(12_000, 1), (24_000, 2)])
def test_ffmpeg_decodes_it_to_the_same_samples(tmp_path, rate, channels):
    x = _radio_audio(BLOCK * 5 + 123, rate, channels, seed=3)
    p = tmp_path / "a.flac"
    p.write_bytes(_encode(x, rate, channels))
    out = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(p), "-f", "s16le", "-"],
                         capture_output=True, check=True)
    assert out.stderr == b""
    got = np.frombuffer(out.stdout, "<i2").reshape(-1, channels)
    assert np.array_equal(got, x.reshape(len(x), channels))
