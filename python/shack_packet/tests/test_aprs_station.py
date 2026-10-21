"""The APRS station: beacons on a schedule within the limits, messages
acknowledged, retried with back-off and deduplicated, the heard table."""

from __future__ import annotations

import pytest
from stations import World

from shack_packet import aprs
from shack_packet.ax25 import Frame
from shack_packet.aprs_station import AprsConfig, AprsStation, Beacon, Fix, SmartBeaconing


def beacon_times(world, engine):
    times = []
    engine.channel.on_transmit = lambda start, end, frames: times.append(start)
    return times


def test_beacons_keep_their_schedule_and_the_floors():
    world = World()
    a = world.station("GM4BRI")
    position = aprs.format_position(55.6190, -4.3470, "/-", "OpenHamShack test")
    station = AprsStation(a, AprsConfig(beacons=[Beacon(position, every_s=600),
                                                 Beacon(aprs.format_status("on 144.800"), every_s=60)]))
    times = beacon_times(world, a)
    world.run(3600)
    # The status asked for every minute; the floor makes it every five, and
    # no two beacons go within a minute of each other.
    assert 8 <= station.stats["beacons"] <= 20
    gaps = [b - a_ for a_, b in zip(times, times[1:])]
    assert min(gaps) >= 59.0
    assert 10 <= times[0] - 1000.0 <= 61.0                      # the start-up jitter
    heard = world.station("G0ABC")
    log = []
    heard.monitors.append(lambda f: log.append(f.monitor()))
    world.run(1200)
    assert any(line.startswith("GM4BRI>APZOHS,WIDE1-1,WIDE2-1:=5537.14N/00420.82W-") for line in log)


def test_smartbeaconing_speeds_up_with_speed_and_pegs_corners():
    smart = SmartBeaconing()
    assert smart.interval(0) == 1800 and smart.interval(120) == 60
    assert smart.interval(45) == pytest.approx(120)
    world = World()
    a = world.station("GM4BRI-9")
    fix = Fix(55.6, -4.3, speed_kmh=45.0, course_deg=90.0)
    AprsStation(a, AprsConfig(beacons=[Beacon(source=lambda: fix, symbol="/>")], min_gap_s=20))
    times = beacon_times(world, a)
    world.run(600)
    steady = len(times)
    assert 4 <= steady <= 6                                     # every 120 s at 45 km/h
    fix.course_deg = 180.0                                      # a right-angle turn
    world.run(40)
    assert len(times) == steady + 1


def _pair(**config):
    world = World(**config)
    a = world.station("GM4BRI")
    b = world.station("G0ABC-9")
    return world, a, b, AprsStation(a), AprsStation(b)


def test_a_message_is_acknowledged_and_delivered_once():
    world, a, b, sa, sb = _pair()
    got = []
    sb.on_message = got.append
    [msg] = sa.send_message("G0ABC-9", "QSY 145.500?")
    assert world.run(120, lambda: msg.state == "acked")
    assert [(m.source, m.text) for m in got] == [("GM4BRI", "QSY 145.500?")]
    assert msg.tries == 1 and sb.stats["acks_sent"] == 1
    kinds = [e.kind for e in a.events]
    assert "aprs-acked" in kinds


def test_a_lost_ack_brings_a_retry_which_is_acked_again_but_not_delivered_twice():
    world, a, b, sa, sb = _pair()
    got = []
    sb.on_message = got.append
    # The first ack is lost: A does not hear B for a minute.
    world.medium.hear("GM4BRI", "G0ABC-9", True)
    [msg] = sa.send_message("G0ABC-9", "are you there")
    assert world.run(20, lambda: sb.stats["acks_sent"] == 1)
    world.medium._deaf.add(("G0ABC-9", "GM4BRI"))               # B's ack never reaches A
    world.run(10)
    world.medium._deaf.discard(("G0ABC-9", "GM4BRI"))
    assert world.run(120, lambda: msg.state == "acked")
    assert msg.tries == 2 and sb.stats["acks_sent"] == 2 and sb.stats["duplicates"] == 1
    assert len(got) == 1


def test_retries_back_off_and_give_up():
    world = World()
    a = world.station("GM4BRI")
    sa = AprsStation(a)
    times = beacon_times(world, a)
    [msg] = sa.send_message("G0NONE", "nobody home")
    assert world.run(2000, lambda: msg.state == "timeout")
    gaps = [round(b - a_) for a_, b in zip(times, times[1:])]
    assert len(times) == 6
    for gap, want in zip(gaps, (30, 60, 120, 240, 480)):
        assert want <= gap <= want + 3
    assert any(e.kind == "aprs-timeout" for e in a.events)


def test_rejected_and_queued_per_addressee():
    world, a, b, sa, sb = _pair()
    first = sa.send_message("G0ABC-9", "one")[0]
    second = sa.send_message("G0ABC-9", "two")[0]
    world.run(1)
    assert first.state == "sent" and second.state == "queued"   # one outstanding per addressee
    b.send_ui("APZOHS", aprs.format_ack("GM4BRI", first.id, reject=True))
    assert world.run(60, lambda: first.state == "rejected")
    assert world.run(120, lambda: second.state == "acked")


def test_long_text_is_split_and_reply_acks_are_read():
    world, a, b, sa, sb = _pair()
    got = []
    sb.on_message = got.append
    parts = sa.send_message("G0ABC-9", "word " * 30)
    assert len(parts) == 3 and all(len(p.text) <= 67 for p in parts)
    assert world.run(400, lambda: all(p.state == "acked" for p in parts))
    assert " ".join(m.text for m in got).split() == ["word"] * 30
    [msg] = sa.send_message("G0ABC-9", "reply-ack test")
    world.run(1)
    b.send_ui("APZOHS", f":GM4BRI   :sure{{AB}}{msg.id}")
    assert world.run(60, lambda: msg.state == "acked")


def test_the_heard_table():
    world = World()
    a = world.station("GM4BRI")
    sa = AprsStation(a)
    b = world.station("G0ABC-9")
    c = world.station("MB7UC")
    b.send_ui("APRS", aprs.format_position(55.5, -4.5, "/>", "mobile"))
    world.run(30)                    # one at a time: sent together, they collide
    c.send_ui("APRS", ">digipeater status")
    world.run(30)
    c.repeat(Frame.from_monitor("M0FAR>APRS,MB7UC*:!5530.00N/00330.00W-far"))
    world.run(30)
    heard = sa.heard
    assert heard["G0ABC-9"].direct and heard["G0ABC-9"].position.lat == pytest.approx(55.5)
    assert heard["G0ABC-9"].symbol == "/>"
    assert heard["MB7UC"].status == "digipeater status"
    assert not heard["M0FAR"].direct and heard["M0FAR"].heard_from == "MB7UC"


def test_a_frame_heard_by_another_receiver_goes_in_the_heard_table_and_is_never_answered():
    """hear() is for the SDR monitor: the station may not even be on the
    channel the SDR heard, so a message to it there gets no ack."""
    world = World()
    me = world.station("GM4BRI")
    station = AprsStation(me)
    sent = []
    me.channel.on_transmit = lambda start, end, frames: sent.append(frames)
    station.hear(Frame.from_monitor("M0ABC-9>APZOHS:!5530.00N/00410.00W>hi"))
    station.hear(Frame.from_monitor("M0ABC-9>APZOHS::GM4BRI   :are you there{07"))
    world.run(30)
    entry = station.heard["M0ABC-9"]
    assert entry.packets == 2 and entry.position.lat == pytest.approx(55.5)
    assert sent == [] and station.stats["acks_sent"] == 0 and station.stats["messages_received"] == 0
