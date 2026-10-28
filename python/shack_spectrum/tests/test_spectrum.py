"""Behaviour the vectors do not spell out: what the server must never do."""
import numpy as np
import pytest

from shack_spectrum import LatestWins, Row, SpectrumError, decode, dequantise, encode, quantise, reduce


def test_peak_never_loses_a_one_bin_carrier_however_far_out():
    src = np.full(4096, -130.0)
    src[1234] = -50.0
    for cols in (1, 7, 100, 1280):
        assert reduce(src, 0.0, 1.0, cols, mode="peak")[2].max() == -50.0


def test_every_bin_lands_in_exactly_one_column():
    # Mean of a ramp of linear powers: if a bin were dropped or counted
    # twice, the power-weighted total would not come back.
    p = np.arange(1, 1001, dtype=float)
    src = 10 * np.log10(p)
    for cols in (1, 3, 37, 999, 1000):
        _, _, db = reduce(src, 0.0, 1.0, cols, mode="mean")
        widths = np.diff((np.arange(len(db) + 1) * 1000) // len(db))
        assert np.sum(10 ** (db / 10) * widths) == pytest.approx(p.sum())


def test_quantise_clamps_and_round_trips_within_half_a_step():
    db = np.array([-200.0, -142.0, -100.0, -46.375, 0.0])
    v = quantise(db, -142.0, 0.375)
    assert v[0] == 0 and v[-1] == 255
    inside = (db >= -142.0) & (db <= -142.0 + 255 * 0.375)
    assert np.all(np.abs(dequantise(v, -142.0, 0.375) - db)[inside] <= 0.1875)


def test_a_slow_client_never_has_more_than_one_row_waiting_per_stream():
    sent = []
    s = LatestWins(lambda c, b: sent.append(decode(b)))
    for t in range(1000):
        s.offer("tv", Row(0, 0, t, 0.0, 1.0, np.array([1], np.uint8)))
    s.drained("tv")
    assert [(r.t_ms, r.gap) for r in sent] == [(0, False), (999, True)]
    assert s.dropped == 998


def test_a_forgotten_client_starts_again_without_a_gap():
    sent = []
    s = LatestWins(lambda c, b: sent.append(decode(b)))
    for t in range(3):
        s.offer("a", Row(0, 0, t, 0.0, 1.0, np.array([1], np.uint8)))
    s.forget("a")
    s.offer("a", Row(0, 0, 9, 0.0, 1.0, np.array([1], np.uint8)))
    assert [(r.seq, r.gap) for r in sent] == [(0, False), (0, False)]


def test_encode_refuses_values_that_are_not_uint8_or_uint16():
    with pytest.raises(SpectrumError) as err:
        encode(Row(0, 0, 0, 0.0, 1.0, np.array([1.5])))
    assert err.value.code == "depth"


def test_reduce_refuses_an_unknown_mode():
    with pytest.raises(SpectrumError) as err:
        reduce([0.0, 1.0], 0.0, 1.0, 1, mode="median")
    assert err.value.code == "reduce"
