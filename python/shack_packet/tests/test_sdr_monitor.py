"""The SDR monitor end to end on synthetic IQ - stations on five channels
at different levels and frequency errors through a receiver whose crystal
is out, a strong neighbour, lost samples, a decoder that falls behind -
and the monitor's tables from frames built by hand."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest
from stations import World

from shack_packet import aprs
from shack_packet.aprs_station import AprsStation
from shack_packet.ax25 import PID_NETROM, Control, Frame, FrameType
from shack_packet.netrom import Route, build_nodes
from shack_packet.sdr import Monitor, Neighbour, Pipeline, Runner, Station, SyntheticSource, default_plan
from shack_packet.sdr.monitor import Decoded
from shack_packet.sdr.plan import Channel, ChannelPlan

RATE = 2_400_000
PLAN = default_plan()
CENTRE = PLAN.centre_for(RATE)


def ui(src, text, via=("WIDE1-1",)):
    return Frame.from_monitor(f"{src}>APZOHS{''.join(',' + v for v in via)}:{text}").encode()


def run(source, ppm=0.0, plan=PLAN, **kw):
    pipe = Pipeline(source.rate, source.centre, plan, ppm=ppm, **kw)
    with source:
        for block in iter(source.read, None):
            pipe.process(block)
    pipe.flush()
    return pipe


def by_channel(pipe):
    out = {c.id: [] for c in pipe.plan}
    for d in pipe.monitor.frames:
        out[d.channel].append(d)
    return out


def test_five_channels_each_decode_their_own_frames_through_a_receiver_30_ppm_out():
    stations = []
    for i, ch in enumerate(PLAN):
        frames = [ui(f"M0T{i}", f">{ch.id} {j}") for j in range(3)]
        # Levels 12 dB apart from strongest to weakest, each transmitter off
        # frequency by its own amount, starting at different moments.
        stations.append(Station(ch.hz, frames, level_db=-30 - 3 * i, offset_hz=(-600, -250, 0, 300, 700)[i],
                                start_s=0.2 + 0.13 * i, every_s=1.1))
    src = SyntheticSource(CENTRE, RATE, stations, noise_dbfs=-35, ppm=30.0, seconds=4.0)
    pipe = run(src, ppm=30.0)
    got = by_channel(pipe)
    for i, ch in enumerate(PLAN):
        assert [d.raw for d in got[ch.id]] == stations[i].frames, ch.id
    levels = [sum(d.level_db for d in got[c.id]) / 3 for c in PLAN]
    assert levels == sorted(levels, reverse=True)
    for i, c in enumerate(PLAN):
        assert levels[i] == pytest.approx(-30 - 3 * i, abs=0.5)
        for d in got[c.id]:
            assert d.offset_hz == pytest.approx(stations[i].offset_hz, abs=40)
            assert d.snr_db == pytest.approx((-30 - 3 * i) - (-35 + 10 * __import__("math").log10(12_500 / RATE)),
                                             abs=3.0)
    # Every frame dated by when it was sent, to within a few bits.
    sent = sorted((s.time_s, s.frame) for s in src.truth)
    t0 = pipe.t0
    heard = sorted((d.time - t0, d.raw) for d in pipe.monitor.frames)
    assert [f for _, f in heard] == [f for _, f in sent]
    assert all(abs(a - b) < 0.05 for (a, _), (b, _) in zip(heard, sent))


def test_without_the_ppm_correction_the_far_channels_are_lost_and_the_monitor_says_by_how_much():
    """30 ppm is 4.3 kHz on 2 m: the signal hangs off the edge of its
    channel filter. A modest error is still decoded, and the frames say
    what to correct by."""
    stations = [Station(c.hz, [ui("M0ABC", f">{c.id} {j}") for j in range(3)], level_db=-35, start_s=0.3)
                for c in PLAN]
    pipe = run(SyntheticSource(CENTRE, RATE, stations, noise_dbfs=-40, ppm=4.0, seconds=4.0))
    assert len(pipe.monitor.frames) == 15
    assert pipe.monitor.ppm_estimate() == pytest.approx(4.0, abs=0.3)
    pipe2 = run(SyntheticSource(CENTRE, RATE, stations, noise_dbfs=-40, ppm=4.0, seconds=4.0),
                ppm=pipe.monitor.ppm_estimate())
    assert all(abs(d.offset_hz) < 30 for d in pipe2.monitor.frames)


def test_a_strong_neighbour_neither_leaks_in_nor_stops_weak_stations_beside_it():
    """The channel filter's part, separated from the neighbour's own
    splatter: a carrier 50 dB above the weak stations, one channel away,
    leaves their noise floor where it was. FM speech that strong would not
    (the synthetic speech's own power in the next channel is -50 dBc,
    measured, much as a real transmitter's -60 dBc limit), so speech is 30
    dB above them; and a strong packet station's frames stay on its own
    channel."""
    plan = ChannelPlan((Channel("lo", 144_925_000), Channel("mid", 144_937_500), Channel("hi", 144_950_000)))
    centre = 145_100_000.0
    weak_lo = Station(144_925_000, [ui("M0LO", f">lo {j}") for j in range(4)], level_db=-60, start_s=0.3)
    weak_hi = Station(144_950_000, [ui("M0HI", f">hi {j}") for j in range(4)], level_db=-60, start_s=0.5)
    quiet = run(SyntheticSource(centre, RATE, [weak_lo, weak_hi], noise_dbfs=-50, seconds=6.5), plan=plan)
    for loud in (Neighbour(144_937_500, level_db=-10, kind="carrier"), Neighbour(144_937_500, level_db=-30)):
        pipe = run(SyntheticSource(centre, RATE, [weak_lo, weak_hi], [loud], noise_dbfs=-50, seconds=6.5), plan=plan)
        got = by_channel(pipe)
        assert [d.raw for d in got["lo"]] == weak_lo.frames
        assert [d.raw for d in got["hi"]] == weak_hi.frames
        assert got["mid"] == []
        if loud.kind == "carrier":
            for c in ("lo", "hi"):
                assert pipe.monitor.channels[c].floor_db == pytest.approx(quiet.monitor.channels[c].floor_db, abs=0.3)
    # Between the strong station's transmissions: while it sends, its own
    # splatter (as any FM transmitter's) is above a station 55 dB down.
    strong = Station(144_937_500, [ui("M0LOUD", f">loud {j}") for j in range(4)], level_db=-5, start_s=0.4)
    between = Station(144_950_000, weak_hi.frames, level_db=-60, start_s=1.1)
    pipe = run(SyntheticSource(centre, RATE, [strong, between], noise_dbfs=-50, seconds=6.5), plan=plan)
    got = by_channel(pipe)
    assert [d.raw for d in got["mid"]] == strong.frames
    assert [d.raw for d in got["hi"]] == weak_hi.frames
    assert got["lo"] == []                                       # the strong station's frames stay on its own channel


def test_fx25_and_preemphasised_stations_are_reported_as_they_were_sent():
    plan = ChannelPlan((Channel("a", 144_800_000), Channel("b", 144_925_000)))
    fx = Station(144_800_000, [ui("M0FX", f">fx25 {j}") for j in range(2)], level_db=-40, fx25=32, start_s=0.2)
    pre = Station(144_925_000, [ui("M0MIC", f">mic {j}") for j in range(2)], level_db=-40, preemphasis=True,
                  start_s=0.4)
    for deemph in (None, 750.0):
        pipe = run(SyntheticSource(145_000_000.0, RATE, [fx, pre], noise_dbfs=-40, seconds=3.5), plan=plan,
                   deemphasis_us=deemph)
        got = by_channel(pipe)
        assert [d.raw for d in got["a"]] == fx.frames
        assert all(d.fx25 is not None and d.fx25_check == 32 for d in got["a"])
        assert [d.raw for d in got["b"]] == pre.frames


def test_lost_samples_lose_only_their_own_frames_and_later_frames_keep_their_time():
    ch = PLAN.channels[0]
    st = Station(ch.hz, [ui("M0GAP", f">gap {j}") for j in range(6)], level_db=-35, start_s=0.2, every_s=1.0)
    src = SyntheticSource(CENTRE, RATE, [st], noise_dbfs=-40, seconds=6.5, drops=[(2.1, 0.8)])
    pipe = run(src)
    got = [d.raw for d in pipe.monitor.frames]
    assert got == [st.frames[i] for i in (0, 1, 3, 4, 5)]          # frame 2 was in the gap
    assert src.stats.dropped == int(0.8 * RATE) and src.stats.drops == 1
    truth = {s.frame: s.time_s for s in src.truth}
    for d in pipe.monitor.frames:
        assert d.time - pipe.t0 == pytest.approx(truth[d.raw], abs=0.05)


class _SlowPipeline(Pipeline):
    """A decoder that cannot keep up for a while (a burst of traffic on a
    slow laptop): the runner must drop, count, and keep dating frames."""

    def __init__(self, *a, stall_until=0, **kw):
        super().__init__(*a, **kw)
        self.stall_until = stall_until
        self.calls = 0

    def process(self, block):
        self.calls += 1
        if block.index < self.stall_until:
            time.sleep(0.2)
        return super().process(block)


class _LiveSynthetic(SyntheticSource):
    live = True

    def _read(self):
        time.sleep(0.0125)         # four times real time, and a reader that never waits
        return super()._read()


def test_a_decoder_that_falls_behind_drops_whole_blocks_and_says_so():
    ch = PLAN.channels[0]
    st = Station(ch.hz, [ui("M0RUN", f">run {j}") for j in range(6)], level_db=-35, start_s=0.2, every_s=1.0)
    src = _LiveSynthetic(CENTRE, RATE, [st], noise_dbfs=-40, seconds=6.5)
    pipe = _SlowPipeline(RATE, CENTRE, PLAN, stall_until=int(0.2 * RATE), spectrum=False)
    ended = threading.Event()
    runner = Runner(src, pipe, queue_s=0.5, on_end=ended.set)
    runner.start()
    assert ended.wait(60)
    runner.stop()
    st_ = runner.status()
    assert st_["droppedBlocks"] > 0 and st_["droppedSamples"] == st_["droppedBlocks"] * src.block
    assert st_["ended"] and st_["error"] is None
    truth = {s.frame: s.time_s for s in src.truth}
    assert pipe.monitor.frames, "frames after the stall are decoded"
    for d in pipe.monitor.frames:
        assert d.time - pipe.t0 == pytest.approx(truth[d.raw], abs=0.05)


def test_a_source_that_fails_stops_the_runner_and_hands_over_the_reason():
    from shack_packet.sdr.source import SdrError

    class Dies(SyntheticSource):
        def _read(self):
            if self.pos > RATE:
                raise SdrError("disconnected", "unplugged")
            return super()._read()

    errors = []
    runner = Runner(Dies(CENTRE, RATE, seconds=None), Pipeline(RATE, CENTRE, PLAN, spectrum=False),
                    on_error=errors.append)
    runner.start()
    assert runner.wait(30)
    runner.stop()
    assert [e.reason for e in errors] == ["disconnected"]
    assert runner.status()["error"] == "disconnected"


# -- the tables, from frames built by hand ---------------------------------------

def feed(mon: Monitor, frame: Frame, t: float, channel="aprs", level=-50.0, snr=20.0, offset=0.0):
    hz = next(c.hz for c in mon.plan if c.id == channel)
    pkt = aprs.parse(frame, now=t) if frame.type is FrameType.UI and frame.pid == 0xF0 else None
    from shack_packet.netrom import parse_nodes
    d = Decoded(mon.next_seq(), t, channel, hz, frame.encode(), frame, level, snr, offset,
                aprs=pkt, nodes=parse_nodes(frame) if frame.type is FrameType.UI else None)
    mon.add(d)
    return d


def test_heard_counts_direct_and_through_which_digipeater():
    mon = Monitor(PLAN)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS,WIDE1-1:>direct"), 100.0, level=-40)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS,MB7UC*,WIDE2-1:>via"), 110.0, level=-70)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS,MB7UC,GB7XX*:>via two"), 120.0, channel="node-144925")
    h = mon.heard["M0ABC-9"]
    assert (h.first, h.last, h.frames, h.direct) == (100.0, 120.0, 3, 1)
    assert dict(h.via) == {"MB7UC": 1, "GB7XX": 1}
    assert h.level_db == -40                     # only a direct frame says how strong the station is
    assert dict(h.channels) == {"aprs": 2, "node-144925": 1}
    assert mon.heard["MB7UC"].level_db == -70    # the digipeater was heard, at that level


def test_nodes_are_found_by_their_broadcasts_their_answers_and_their_beacons():
    mon = Monitor(PLAN)
    routes = [Route("GB7AAA", "AAA", "GB7BBB", 192), Route("GB7CCC-1", "CCC", "GB7BBB", 150)]
    feed(mon, build_nodes("GB7BBB", "BBB", routes), 1.0, channel="node-144950")
    feed(mon, Frame.build("GB7NOD", "M0ABC", Control(FrameType.UA, pf=True), command=False), 2.0,
         channel="node-144925")
    feed(mon, Frame.build("GB7NET", "GB7BBB", Control(FrameType.I, False, 0, 0), b"\x00", pid=PID_NETROM), 3.0,
         channel="node-144925")
    feed(mon, Frame.from_monitor("GB7MBX>ID:GB7MBX BBS and node"), 4.0)
    feed(mon, Frame.from_monitor("M0ABC>APZOHS:>no node here"), 5.0)
    n = mon.nodes
    assert n["GB7BBB"].roles == {"netrom-node"} and n["GB7BBB"].alias == "BBB"
    assert n["GB7BBB"].routes == routes
    assert n["GB7NOD"].roles == {"connected", "answers"}
    assert n["GB7NET"].roles == {"connected", "netrom"}
    assert n["GB7MBX"].roles == {"bbs"}
    assert "M0ABC" not in n


def test_links_say_who_talks_to_whom_each_way():
    mon = Monitor(PLAN)
    sabm = Frame.build("M0ABC", "GB7NOD", Control(FrameType.SABM, pf=True))
    ua = Frame.build("GB7NOD", "M0ABC", Control(FrameType.UA, pf=True), command=False)
    i = Frame.build("M0ABC", "GB7NOD", Control(FrameType.I, False, 0, 0), b"hello")
    for k, f in enumerate((sabm, ua, i)):
        feed(mon, f, 10.0 + k, channel="node-144925")
    feed(mon, Frame.from_monitor("M0ABC>APZOHS::G4XYZ    :hello there{01"), 20.0)
    link = mon.links[("GB7NOD", "M0ABC", "ax25")]
    assert (link.a, link.b, link.a_to_b, link.b_to_a) == ("GB7NOD", "M0ABC", 1, 2)
    assert dict(link.types) == {"SABM": 1, "UA": 1, "I": 1}
    msg = mon.links[("G4XYZ", "M0ABC", "aprs-message")]
    assert (msg.a_to_b, msg.b_to_a) == (0, 1)


def test_positions_keep_the_latest_and_a_track_and_objects_by_name():
    mon = Monitor(PLAN)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS:!5530.00N/00410.00W>moving"), 1.0)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS:!5531.00N/00410.00W>moving"), 2.0)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS:!5531.00N/00410.00W>still"), 3.0)
    feed(mon, Frame.from_monitor("GB7XX>APZOHS:;LEADHILLS*111111z5524.00N/00345.00W-event"), 4.0)
    p = mon.positions["M0ABC-9"]
    assert (p.lat, p.time, p.comment) == (pytest.approx(55.5167, abs=1e-3), 3.0, "still")
    assert [round(t[1], 3) for t in p.track] == [55.5, 55.517]
    obj = mon.positions["LEADHILLS"]
    assert obj.source == "GB7XX" and obj.lat == pytest.approx(55.4, abs=1e-3)


def test_activity_buckets_count_frames_airtime_and_busy_time():
    mon = Monitor(PLAN, bucket_s=60, buckets=3)
    for t in (5.0, 30.0, 70.0, 200.0, 250.0):
        feed(mon, Frame.from_monitor("M0ABC>APZOHS:>x"), t)
    mon.busy("aprs", 31.0, 0.5)
    q = list(mon.channels["aprs"].activity)
    assert [b.start for b in q] == [60.0, 180.0, 240.0]          # three buckets kept
    assert [b.frames for b in q] == [1, 1, 1]
    assert q[0].airtime_s == pytest.approx(len(Frame.from_monitor("M0ABC>APZOHS:>x").encode()) * 8 / 1200, rel=0.4)


def test_the_snapshot_is_plain_data():
    mon = Monitor(PLAN)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS:!5530.00N/00410.00W>x"), 1.0)
    feed(mon, build_nodes("GB7BBB", "BBB", [Route("GB7AAA", "AAA", "GB7BBB", 192)]), 2.0, channel="node-144950")
    snap = mon.snapshot()
    again = json.loads(json.dumps(snap))
    assert again["frames"][0]["source"] == "M0ABC-9"
    assert again["nodes"][0]["routes"][0]["destination"] == "GB7AAA"
    assert [c["id"] for c in again["channels"]] == [c.id for c in PLAN]


def test_frames_feed_the_engines_heard_table_but_are_never_answered():
    world = World()
    me = world.station("GM4BRI")
    station = AprsStation(me)
    sent = []
    me.channel.on_transmit = lambda start, end, frames: sent.append(frames)
    mon = Monitor(PLAN)
    mon.feed_heard(station)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS:!5530.00N/00410.00W>hi"), 1.0)
    feed(mon, Frame.from_monitor("M0ABC-9>APZOHS::GM4BRI   :are you there{07"), 2.0)
    world.run(30)
    assert station.heard["M0ABC-9"].packets == 2
    assert station.heard["M0ABC-9"].position.lat == pytest.approx(55.5)
    assert sent == [] and station.stats["acks_sent"] == 0


def test_the_sdr_package_imports_nothing_that_can_key_a_radio():
    code = ("import sys, pkgutil, importlib, shack_packet.sdr as s\n"
            "for m in pkgutil.iter_modules(s.__path__): importlib.import_module('shack_packet.sdr.' + m.name)\n"
            "bad = [m for m in sys.modules if m.startswith(('shack_packet.radios', 'shack_rig'))]\n"
            "print(bad)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
    from shack_packet import sdr
    for cls in (sdr.RtlSdrSource, sdr.RtlTcpSource, sdr.PlutoSource, sdr.FileSource, sdr.SyntheticSource):
        assert not [n for n in dir(cls) if n in ("key", "unkey", "send_pcm", "send_frames", "transmit", "write")
                    or n.startswith("tx_")]
