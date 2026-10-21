"""tools/fm_gap_check.py on made-up captures: a clean over has no holes or
jumps; a starved DAC's 30 ms hole and a buffer's phase slip are found where
they were put."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from shack_packet import afsk
from shack_packet.ax25 import Frame
from shack_packet.radios.pluto import FmModulator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import fm_gap_check  # noqa: E402

RATE = 1_920_000
F = 144_937_500


def capture(hole_at=None, slip_at=None):
    frames = [Frame.from_monitor(f"GM4BRI-2>GM4BRI-1:>{'y' * 200} {i}").encode() for i in range(4)]
    audio = afsk.modulate(frames, 12_000, amplitude=0.5, txdelay_ms=300)
    iq = FmModulator(RATE, 3_000.0, 0.0 + 48_000)(audio)            # the channel 48 kHz above the centre
    if slip_at is not None:
        k = int(slip_at * RATE)
        iq[k:] *= np.exp(1j * 2.0)                                   # a phase jump between two buffers
    if hole_at is not None:
        k = int(hole_at * RATE)
        iq[k:k + int(0.03 * RATE)] = 0
    rng = np.random.default_rng(3)
    pad = int(0.5 * RATE)
    z = np.concatenate([np.zeros(pad), iq, np.zeros(pad)]).astype(np.complex64)
    z += (rng.normal(0, 0.003, len(z)) + 1j * rng.normal(0, 0.003, len(z))).astype(np.complex64)
    return z, len(iq) / RATE


def test_a_clean_over_has_neither():
    z, secs = capture()
    overs = fm_gap_check.analyse(z, RATE, F - 48_000, F)
    assert len(overs) == 1 and abs(overs[0]["length_s"] - secs) < 0.05
    assert overs[0]["holes"] == [] and overs[0]["jumps"] == []


def test_a_hole_and_a_slip_are_found_where_they_are():
    z, _ = capture(hole_at=1.5)
    (o,) = fm_gap_check.analyse(z, RATE, F - 48_000, F)
    assert len(o["holes"]) == 1 and abs(o["holes"][0][0] - (0.5 + 1.5)) < 0.01 and 25 <= o["holes"][0][1] <= 35
    z, _ = capture(slip_at=2.0)
    (o,) = fm_gap_check.analyse(z, RATE, F - 48_000, F)
    assert o["holes"] == [] and any(abs(t - 2.5) < 0.01 for t in o["jumps"])


def test_it_reads_cs16(tmp_path):
    z, _ = capture()
    raw = np.round(np.column_stack([z.real, z.imag]).ravel() * 30000).astype(np.int16)
    p = tmp_path / "b.cs16"
    p.write_bytes(raw.tobytes())
    w, rate, centre = fm_gap_check.load(p, "cs16", RATE, F - 48_000)
    assert len(fm_gap_check.analyse(w, rate, centre, F)) == 1
