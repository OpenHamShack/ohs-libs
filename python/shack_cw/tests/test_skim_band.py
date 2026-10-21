"""The skimmer on synthetic bands (shack_cw.skim.synth): many stations at
once, scored against what was rendered - the spots, their frequency,
speed and SNR, the channel limit, carriers, and the busted-call trap."""
import numpy as np
import pytest

from shack_cw.skim import Birdies, Skimmer, bench, detect, synth

RATE = 24_000.0
LIM = 0.42 * RATE


def country(call):
    # Every call of synth.CALLS, and the trap's: shape is checked by calls.
    return {"country": "x", "dxcc": call[:2], "continent": "EU"}


def run(z, **kw):
    kw.setdefault("country", country)
    return bench.run(z, RATE, lo=-LIM, hi=LIM, **kw)


@pytest.fixture(scope="module")
def band():
    st = synth.random_band(12, 2 * LIM - 1500, seed=4, min_gap=300.0, snr=(12.0, 30.0))
    z = synth.render(st, 45.0, RATE, seed=4, carriers=[(3210.0, 25.0)], static=1.0)
    return st, z, run(z)


def test_spots_on_a_band_of_twelve(band):
    st, _, (sk, items, _) = band
    r = bench.score_synth(st, sk.calls.list(45.0), items)
    assert r["recall"] >= 0.8, r
    assert r["precision"] == 1.0 and r["busted_spotted"] == 0, r
    assert r["freq_err_max"] <= 10.0, r
    assert r["wpm_err_median"] <= 0.1, r


def test_snr_is_the_rbns_measure(band):
    st, _, (sk, _, _) = band
    truth = {s.call: s for s in st if not s.qsb}
    got = [(truth[s["call"]].snr, s["snr"]) for s in sk.calls.list(45.0) if s["call"] in truth]
    assert got
    for want, have in got:
        assert abs(have - want) <= 3.0, got


def test_carrier_is_listed_not_decoded(band):
    _, _, (sk, _, _) = band
    snap = sk.snapshot(45.0)
    assert any(abs(c["f"] - 3210.0) < 10 for c in snap["carriers"])
    assert not any(abs(c["f"] - 3210.0) < 40 for c in snap["channels"])
    assert all(c["decoding"] for c in snap["channels"][:5])
    assert snap["spotsRev"] == sk.calls.rev


def test_channel_limit_keeps_the_strongest(band):
    st, z, _ = band
    sk, _, _ = run(z, max_channels=4)
    assert sk.decoding <= 4
    strongest = sorted(st, key=lambda s: -s.snr)[:3]
    held = [sk.tracker.tracks[k].f for k in sk.pool.channels]
    for s in strongest:
        assert any(abs(f - s.offset) < 40 for f in held), (s.call, held)


def test_close_stations_and_the_busted_trap():
    # Two stations 100 Hz apart, and two with Morse-alike calls 2 kHz
    # apart: all four spotted, under their own calls, none busted.
    st = [synth.Station("DL1ABC", -3000.0, 24.0, 22.0), synth.Station("OK1AB", -2900.0, 30.0, 20.0, "test"),
          synth.Station("GM4BRI", 1000.0, 22.0, 20.0), synth.Station("GM4BRS", 3000.0, 26.0, 20.0)]
    z = synth.render(st, 40.0, RATE, seed=9)
    sk, items, _ = run(z)
    got = {s["call"]: s for s in sk.calls.list(40.0)}
    for s in st:
        assert s.call in got and got[s.call]["status"] == "valid", (s.call, got.get(s.call))
        assert abs(got[s.call]["f"] - s.offset) <= 10.0


def test_drift_is_followed_as_one_track():
    st = [synth.Station("DL1ABC", -2000.0, 24.0, 20.0, drift=1.0)]
    z = synth.render(st, 40.0, RATE, seed=5)
    sk, _, _ = run(z)
    tracks = [tr for tr in sk.tracker.tracks.values() if not tr.carrier]
    assert len(tracks) == 1, [(tr.id, tr.f) for tr in tracks]
    assert abs(tracks[0].f - (-2000.0 + 40.0)) < 15.0
    assert [s["call"] for s in sk.calls.list(40.0) if s["status"] == "valid"] == ["DL1ABC"]


def test_dc_notch_and_birdies(tmp_path, monkeypatch):
    # A steady line at the receiver's own centre is never offered a
    # decoder; one that holds for BIRDIE_S joins the birdie list, which
    # outlives the skimmer.
    monkeypatch.setattr(detect, "BIRDIE_S", 15.0)
    z = synth.render([], 40.0, RATE, seed=2, carriers=[(1500.0, 25.0)], dc_db=30.0)
    path = tmp_path / "birdies-20m.json"
    sk = Skimmer(RATE, -LIM, LIM, 0.0, birdies=Birdies(path), dc=0.0)
    for i in range(0, len(z), 1800):
        sk.feed(z[i:i + 1800], t=i / RATE)
    assert not any(abs(tr.f) < 500 for tr in sk.tracker.tracks.values())
    assert Birdies(path).near(1500.0)
    sk2 = Skimmer(RATE, -LIM, LIM, 0.0, birdies=Birdies(path))
    for i in range(0, len(z), 1800):
        sk2.feed(z[i:i + 1800], t=i / RATE)
    assert not any(abs(tr.f - 1500.0) < 20 for tr in sk2.tracker.tracks.values())


def test_a_new_centre_restarts_the_tracks_and_keeps_the_spots():
    z, st = synth.selftest_band(RATE, 30.0)
    sk = Skimmer(RATE, -LIM, LIM, 0.0, country=country, band="20m")
    for i in range(0, len(z), 1800):
        sk.feed(z[i:i + 1800], t=i / RATE)
    spots = sk.spots()
    assert spots and sk.signals
    sk.feed(z[:1800], t=30.0, centre=500.0)
    assert sk.signals == 0 and sk.decoding == 0
    assert [s["call"] for s in sk.spots()] == [s["call"] for s in spots]
    sk.feed(z[:1800], t=30.1, centre=500.0, generation=2)
    sk.feed(z[:1800], t=30.2, centre=500.0, generation=3)
    assert sk.signals == 0


def test_noise_alone_makes_no_spot():
    z = synth.render([], 60.0, RATE, seed=11, static=4.0)
    sk, items, _ = run(z)
    assert items == []
    assert all(not c["text"].strip() for c in sk.snapshot()["channels"]), sk.snapshot()["channels"]


def test_selftest_band_decodes():
    z, st = synth.selftest_band(RATE, 40.0)
    sk, items, _ = run(z)
    assert {s["call"] for s in sk.calls.list(40.0) if s["status"] == "valid"} == {s.call for s in st}


def test_a_comb_of_steady_lines_gets_no_decoders():
    # ohs-box's 20 m recording carries an RFI comb: 38 lines exactly 240 Hz
    # apart across the band, from a local switching source. Here 30 of
    # them, -2 to +3 dB over the noise in 500 Hz (8-13 dB in a detection
    # bin: weak enough that the noise moves their power, so a strong
    # carrier's index rule alone took every one for keyed CW and gave each a
    # decoder), beside three keyed stations: the lines are carriers, the
    # comb is found, and only the stations hold decoders.
    rng = np.random.default_rng(3)
    comb = [(-7000.0 + 240.0 * k + 37.0, float(rng.uniform(-2.0, 3.0))) for k in range(30)]
    st = [synth.Station("DL1ABC", -7700.0, 24.0, 20.0), synth.Station("OK1AB", 1500.0, 28.0, 20.0, "test"),
          synth.Station("F5NQL", 4000.0, 20.0, 18.0)]
    z = synth.render(st, 40.0, RATE, seed=3, carriers=comb)
    sk, _, _ = run(z)
    held = [sk.tracker.tracks[k].f for k in sk.pool.channels]
    on_comb = [f for f in held if min(abs(f - c) for c, _ in comb) < 15]
    assert on_comb == [], (on_comb, len(held))
    assert sk.tracker.comb is not None and abs(sk.tracker.comb[0] - 240.0) < 1.0, sk.tracker.comb
    assert len(sk.snapshot(40.0)["carriers"]) >= 20
    assert {s["call"] for s in sk.calls.list(40.0) if s["status"] == "valid"} == {s.call for s in st}
