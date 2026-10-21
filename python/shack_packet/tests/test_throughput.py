"""Why the fast modes reached only 39 % of the baud on the air, and what
changed (README, throughput): TXDELAY's floor in bits rather than a fixed
80 ms, busy averaged for as long as a wide channel needs and no longer,
and the frames-mode channel's model of a Pluto's timing (sim.RadioTiming),
checked against the keyed run it was taken from."""

from __future__ import annotations

import pytest

from shack_packet import fsk_bench, modem
from shack_packet.channel import ChannelAccess, ChannelConfig
from shack_packet.identity import Station
from shack_packet.radios.pluto import FSK_RATE_IQ
from shack_packet.radios.sdrrx import FskChannelReceiver
from shack_packet.sim import PLUTO, Medium, RadioTiming, SimClock


class _Link:
    turnaround_s = 0.03
    baud = 1200
    mode = None


def test_txdelay_floor_is_the_turnaround_plus_the_modes_lock_bits():
    link = _Link()
    station = Station.of("GM4BRI", tx_enabled=True)
    for mode, floor in (("afsk1200", 0.03 + 0.08), ("g3ruh9600", 0.03 + 96 / 9600),
                        ("gfsk153600", 0.03 + 96 / 153_600), ("4fsk76800", 0.03 + 384 / 153_600)):
        ch = ChannelAccess(station, link, ChannelConfig(txdelay_s=0.0, mode=mode))
        assert ch.txdelay_s == pytest.approx(floor)
        # A longer TXDELAY asked for is kept.
        assert ChannelAccess(station, link, ChannelConfig(txdelay_s=0.2, mode=mode)).txdelay_s == 0.2


def test_busy_averages_as_many_samples_as_a_narrow_channel_and_no_longer():
    def average(mode):
        m = modem.mode(mode)
        return FskChannelReceiver(FSK_RATE_IQ, 1e9 - 2e5, 1e9, baud=m.baud, deviation_hz=m.deviation_hz,
                                  occupied_hz=m.occupied_hz).busy_average_s
    assert average("g3ruh9600") == pytest.approx(0.1)
    assert average("g3ruh38400") == pytest.approx(0.03, abs=0.002)
    assert average("gfsk153600") == average("4fsk76800") == pytest.approx(0.02)


class _Sink:
    def __init__(self, clock):
        self.clock = clock
        self.frames = []
        self.busy = []

    def frame_in(self, frame):
        self.frames.append((round(self.clock.now, 4), frame))

    def busy_in(self, busy):
        self.busy.append((round(self.clock.now, 4), busy))


def _medium(timing):
    clock = SimClock(0.0)
    medium = Medium(clock, "frames", baud=153_600, delay_s=0.0, dcd_lag_s=0.0, min_lock_s=0.001)
    a, b = medium.radio("A", timing=timing), medium.radio("B", timing=timing)
    for r in (a, b):
        r.attach(_Sink(clock))
    return clock, medium, a, b


def _run(clock, medium, until):
    while clock.now < until:
        nxt = medium.poll(clock.now)
        clock.now = min(until, nxt) if nxt is not None and nxt > clock.now else until


def test_a_plutos_timing_delays_frames_and_busy_and_deafens_it_after_its_own_over():
    timing = RadioTiming(tx_lag_s=0.03, drain_s=0.05, rx_lag_s=0.075, busy_tail_s=0.15, rx_resume_s=0.085)
    clock, medium, a, b = _medium(timing)
    a.key()
    a.send_frames([b"one"], 0.01, 0.1)              # on the air 0.03-0.13, carrier to 0.18
    _run(clock, medium, 0.2)
    # B answers at 0.2, before A is listening again (A's carrier dropped
    # at 0.18, it hears from 0.265): a preamble that ends first is lost.
    b.key()
    b.send_frames([b"lost"], 0.02, 0.05)            # 0.23-0.28, carrier to 0.33: preamble to 0.25
    b.send_frames([b"kept"], 0.05, 0.08)            # after its own last over: preamble 0.33-0.38
    _run(clock, medium, 2.0)
    assert b.sink.frames == [(0.205, b"one")]       # 0.13 + rx_lag
    assert b.sink.busy == [(0.03, True), (0.33, False)]   # carrier off at 0.18 + busy tail
    assert [f for _t, f in a.sink.frames] == [b"kept"]


def test_without_a_timing_the_medium_is_as_it_was():
    clock, medium, a, b = _medium(None)
    a.key()
    a.send_frames([b"one"], 0.01, 0.1)
    _run(clock, medium, 1.0)
    assert b.sink.frames == [(0.1, b"one")] and b.sink.busy == [(0.0, True), (0.1, False)]


def test_the_model_reproduces_the_keyed_run_and_the_tuned_settings_beat_it():
    """sim.PLUTO came from the keyed run's own timing: 10 KB at 38 400 baud
    as keyed took 5.5 s on the air. The tuned settings (k 63, PACLEN 512,
    SREJ, the TXDELAY floor, the shorter busy) at 153 600 baud must at
    least double what the keyed settings make of it."""
    ok, t = fsk_bench.transfer("g3ruh38400", 10, **fsk_bench.AS_KEYED)
    assert ok and 4.5 < t < 6.0, t
    ok, slow = fsk_bench.transfer("gfsk153600", 50, **fsk_bench.AS_KEYED)
    ok2, fast = fsk_bench.transfer("gfsk153600", 50, **fsk_bench.tuned("gfsk153600"))
    assert ok and ok2 and fast < slow / 2, (slow, fast)
    assert PLUTO.busy_tail_s > fsk_bench.tuned("gfsk153600")["timing"].busy_tail_s
