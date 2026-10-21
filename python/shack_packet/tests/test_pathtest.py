"""The path test (pathtest.py): a sender's numbered frames per mode, a
receiver hearing every mode at once and counting them, the report, the
best mode seeding adaptation, and the drive-level steps."""

from __future__ import annotations

import random

import pytest

from shack_packet import Station, adapt, fsk_bench, pathtest
from shack_packet.ax25 import Frame
from shack_packet.channel import ChannelConfig
from shack_packet.datalink import LinkConfig
from shack_packet.engine import Engine
from shack_packet.sim import PLUTO, Medium, SimClock, Simulation

LADDER = adapt.ladder()


def test_frames_say_their_run_mode_and_number_and_others_are_ignored():
    frames = list(pathtest.plan(["g3ruh9600", "4fsk76800"], count=3))
    assert [(m, n) for m, n, _ in frames] == [("g3ruh9600", 1), ("g3ruh9600", 2), ("g3ruh9600", 3),
                                              ("4fsk76800", 1), ("4fsk76800", 2), ("4fsk76800", 3)]
    run = frames[0][2].split()[1]
    assert pathtest.parse(frames[4][2]) == (run, "4fsk76800", 2, 3)
    assert pathtest.parse(">ordinary APRS") is None and pathtest.parse("OHSPT 1 nomode 1/2") is None


def test_the_tally_counts_each_mode_and_names_the_best():
    t = pathtest.Tally()
    for mode, n in (("4fsk76800", 20), ("gfsk76800", 49), ("g3ruh9600", 50)):
        for seq in range(1, n + 1):
            f = Frame.from_monitor(f"GM4BRI-1>APZOHS:{pathtest.frame_info('120000', mode, seq, 50, pad=5)}")
            assert t.heard(f, cn0=66.0, offset_hz=-2500.0, now=float(seq))
    t.set_bad_fcs({"4fsk76800": 31})
    rows = t.rows()
    assert [r["mode"] for r in rows] == ["4fsk76800", "gfsk76800", "g3ruh9600"]
    assert rows[0]["missing"] == 30 and rows[0]["bad_fcs"] == 31 and rows[1]["pct"] == 98.0
    assert t.best_mode() == "gfsk76800"
    text = t.report(frequency_hz=1_299_525_000, station="GM4BRI-2")
    assert "1299.5250 MHz" in text and "best mode this way: gfsk76800" in text and "-2500" in text


def test_a_one_way_test_on_the_sim_seeds_the_receivers_adaptation():
    """One way, as with the cable moved by hand: A sends 20 of each mode,
    B hears every mode at once; at 62 dB-Hz 4fsk76800 hardly gets through
    and gfsk76800 does. B then starts a connection asking for gfsk76800."""
    clock = SimClock()
    medium = Medium(clock, "frames", seed=2, quality=fsk_bench.fading(62.0, 62.0), min_lock_s=0.001)
    sim = Simulation(clock, medium)
    engines = {}
    for call in ("GM4BRI-1", "GM4BRI-2"):
        radio = medium.radio(call, turnaround_s=0.03, timing=PLUTO, modes=LADDER)
        engines[call] = Engine(Station.of(call, tx_enabled=True), radio, clock=clock,
                               channel=ChannelConfig(persist=255, txdelay_s=0.0),
                               link=LinkConfig(k=32, n1=256, modulo=128, srej=True), rng=random.Random(call))
        sim.add(engines[call])
    a, b = engines["GM4BRI-1"], engines["GM4BRI-2"]
    b.listen_also(LADDER)
    tally = pathtest.Tally()
    tally.expect(["4fsk76800", "gfsk76800", "g3ruh9600"], 20)
    b.monitors.append(lambda f: tally.heard(f, cn0=b.last_heard[1]))
    for mode, _seq, info in pathtest.plan(["4fsk76800", "gfsk76800", "g3ruh9600"], count=20, pad=50):
        a.send_ui("APZOHS", info, mode=mode)
        sim.run(0.3)
    sim.run(5)
    rows = {r["mode"]: r for r in tally.rows()}
    assert rows["4fsk76800"]["pct"] < 50 and rows["gfsk76800"]["pct"] >= 90, rows
    assert rows["gfsk76800"]["cn0_dbhz"] == pytest.approx(62.0, abs=2)
    best = tally.best_mode()
    assert best == "gfsk76800"
    b.link_config = LinkConfig(k=32, n1=256, modulo=128, srej=True, adapt=LADDER, adapt_start=best)
    a.listen("GM4BRI-1", lambda call: True)
    conn = b.connect("GM4BRI-1")
    sim.run(10, lambda: conn.connected)
    assert conn.connected and conn.link.adapter.request == "gfsk76800"
    them = next(iter(a.connections.values()))
    assert sim.run(10, lambda: them.link.tx_mode == "gfsk76800")


def test_drive_steps_climb_from_minus_30_and_stop_at_the_cap():
    assert pathtest.drive_steps() == [float(x) for x in range(-30, 1, 2)]
    assert pathtest.drive_steps(step_db=3, cap_db=-20)[-1] == -21.0
    with pytest.raises(ValueError):
        pathtest.drive_steps(step_db=0)
