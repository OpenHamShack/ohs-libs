"""Recordings written from a source and played back as they were heard:
SigMF with its time and its gaps, and raw files at full scale."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest
from sdrhelp import RATE, band, decode, texts

from shack_packet.sdr import FileSource, SdrError, record
from shack_packet.sdr.source import complex_to_cu8


def test_a_recording_plays_back_as_heard_with_its_time_and_its_gap(tmp_path):
    live = band(seconds=3.6, drops=[(1.3, 0.5)])
    data = record(live, tmp_path / "evening", 10.0, author="GM4BRI")
    meta = json.loads(data.with_suffix(".sigmf-meta").read_text())
    assert meta["global"]["core:datatype"] == "ci16_le" and meta["global"]["core:sample_rate"] == RATE
    assert [c["core:sample_start"] for c in meta["captures"]] == [0, int(1.3 * RATE)]
    pipe = decode(FileSource(data))
    played = decode(band(seconds=3.6, drops=[(1.3, 0.5)]))
    # ">one 1" was sent inside the gap; ">two 1" only lost its preamble.
    assert texts(pipe) == texts(played) == [">one 0", ">two 0", ">two 1"]
    src = FileSource(data)
    with src:
        first = src.read()
        while (b := src.read()) is not None and not b.dropped:
            pass
    assert b.dropped == pytest.approx(0.5 * RATE, abs=RATE * 0.002)
    stamp = meta["captures"][0]["core:datetime"]
    assert first.time == pytest.approx(dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    assert b.time - first.time == pytest.approx(1.8, abs=0.01)


@pytest.mark.parametrize("suffix,dtype", [(".cu8", "cu8"), (".cs16", "ci16_le"), (".cf32", "cf32_le")])
def test_raw_files_play_at_full_scale(tmp_path, suffix, dtype):
    z = (0.5 * np.exp(2j * np.pi * np.arange(4800) / 48)).astype(np.complex64)
    path = tmp_path / ("iq" + suffix)
    if dtype == "cu8":
        path.write_bytes(complex_to_cu8(z))
    elif dtype == "ci16_le":
        s = np.empty(2 * len(z), "<i2")
        s[0::2], s[1::2] = np.rint(z.real * 32768), np.rint(z.imag * 32768)
        path.write_bytes(s.tobytes())
    else:
        path.write_bytes(z.tobytes())
    src = FileSource(path, rate=48_000, centre=145e6)
    with src:
        got = np.concatenate([b.samples for b in iter(src.read, None)])
    assert np.allclose(got, z, atol=0.01)


def test_a_file_that_cannot_be_played_says_why(tmp_path):
    (tmp_path / "x.cu8").write_bytes(b"\x80" * 100)
    with pytest.raises(SdrError) as e:
        FileSource(tmp_path / "x.cu8")
    assert e.value.reason == "bad-file"
    (tmp_path / "y.sigmf-meta").write_text(json.dumps({"global": {"core:datatype": "ri16_le",
                                                                  "core:sample_rate": 1e6}, "captures": []}))
    (tmp_path / "y.sigmf-data").write_bytes(b"")
    with pytest.raises(SdrError) as e:
        FileSource(tmp_path / "y.sigmf-data")
    assert e.value.reason == "bad-file"
