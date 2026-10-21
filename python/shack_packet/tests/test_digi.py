"""The digipeater: the path rules, duplicate suppression on a shared
channel, and a connection made through it."""

from __future__ import annotations

import pytest
from stations import World, accept_all, collect

from shack_packet.ax25 import Control, Frame, FrameType
from shack_packet.datalink import LinkConfig, State
from shack_packet.digi import DigiConfig, Digipeater


def path_after(line, **config):
    world = World()
    engine = world.station("GM4BRI-1")
    digi = Digipeater(engine, DigiConfig(enabled=True, **config))
    out = digi.decide(Frame.from_monitor(line))
    return None if out is None else ",".join(out.path)


@pytest.mark.parametrize("path, expected", [
    ("WIDE1-1,WIDE2-1", "GM4BRI-1*,WIDE2-1"),
    ("WIDE2-2", "GM4BRI-1*,WIDE2-1"),
    ("WIDE2-1", "GM4BRI-1*"),
    ("MB7UC*,WIDE2-1", "MB7UC*,GM4BRI-1*"),
    ("GM4BRI-1,GB7AY", "GM4BRI-1*,GB7AY"),
    ("GM4BRI-1*,WIDE2-1", "GM4BRI-1*,GM4BRI-1*"),     # already repeated by us: the next hop is WIDE2-1
    ("WIDE3-3", None),
    ("WIDE2-3", None),                                 # N above n is not New-N
    ("GB7AY,WIDE2-1", None),                           # the next hop is someone else's
    ("MB7UC*", None),                                  # nothing left
    ("", None),
])
def test_path_rules(path, expected):
    line = "G0ABC-9>APRS" + ("," + path if path else "") + ":>hello"
    assert path_after(line) == expected


def test_aliases_traps_fill_in_and_preemption():
    assert path_after("G0ABC>APRS,GLA,WIDE2-1:>x", aliases=["GLA"]) == "GM4BRI-1*,WIDE2-1"
    assert path_after("G0ABC>APRS,WIDE3-3:>x", wide_excess="trap") == "GM4BRI-1*"
    assert path_after("G0ABC>APRS,WIDE2-2:>x", fill_in=True) is None
    assert path_after("G0ABC>APRS,WIDE1-1:>x", fill_in=True) == "GM4BRI-1*"
    assert path_after("G0ABC>APRS,GB7AY,GM4BRI-1,WIDE2-1:>x") is None
    assert path_after("G0ABC>APRS,GB7AY,GM4BRI-1,WIDE2-1:>x", preempt="drop") == "GM4BRI-1*,WIDE2-1"
    assert path_after("G0ABC>APRS,GB7AY,GM4BRI-1,WIDE2-1:>x", preempt="mark") == "GB7AY*,GM4BRI-1*,WIDE2-1"
    assert path_after("G0ABC>APRS,GB7AY,WIDE2-1:>x", preempt="drop") is None   # never for WIDEn-N
    assert path_after("G0ABC>APRS,WIDE1-1:>x", wide=False) is None


def test_off_by_default_and_never_our_own_frames():
    world = World()
    a = world.station("G0ABC")
    d = world.station("GM4BRI-1")
    Digipeater(d)                                      # not enabled
    a.send_ui("APRS", ">hello", via=["WIDE2-2"])
    world.run(20)
    assert d.channel.stats.transmissions == 0
    on = Digipeater(d, DigiConfig(enabled=True))
    assert on.decide(Frame.from_monitor("GM4BRI-1>APRS,WIDE2-2:>mine")) is not None
    d.send_ui("APRS", ">mine", via=["WIDE2-2"])
    world.run(20)
    assert on.stats["repeated"] == 0 and d.channel.stats.transmissions == 1


def test_two_digipeaters_that_hear_each_other_do_not_ping_pong():
    """WIDE3-3 between two digipeaters in range of each other: without
    duplicate suppression each would repeat the other's repeat until the
    hops ran out."""
    world = World(seed=4)
    a = world.station("G0ABC-9")
    d1 = world.station("MB7UA")
    d2 = world.station("MB7UB")
    world.medium.hear("G0ABC-9", "MB7UB", False)        # only MB7UA hears the sender
    for d in (d1, d2):
        Digipeater(d, DigiConfig(enabled=True, wide_max=3))
    heard = []
    a.monitors.append(lambda f: heard.append(f.monitor()))
    a.send_ui("APRS", ">wide3-3 test", via=["WIDE3-3"])
    world.run(60)
    assert d1.channel.stats.transmissions == 1 and d2.channel.stats.transmissions == 1
    assert d1.digipeater.stats["duplicate"] == 1        # MB7UB's repeat came back with WIDE3-1 left
    assert heard == ["G0ABC-9>APRS,MB7UA*,WIDE3-2:>wide3-3 test"]


def test_a_repeated_i_frame_is_not_a_duplicate():
    world = World()
    d = world.station("GM4BRI-1")
    digi = Digipeater(d, DigiConfig(enabled=True))
    for ns in (0, 1, 0):
        frame = Frame.build("G0ABC", "GB7BBS", Control(FrameType.I, False, 0, ns), b"x" * 50, via=["GM4BRI-1"])
        digi.heard(frame, world.clock.now)
    assert digi.stats["repeated"] == 3


@pytest.mark.parametrize("modulo", [8, 128])
def test_a_connection_through_a_digipeater_to_a_hidden_station(modulo):
    world = World(loss=0.03, seed=modulo)
    a = world.station("G0ABC", link=LinkConfig(modulo=modulo))
    b = world.station("GM4BRI-1")
    c = world.station("GB7BBS")
    world.medium.hear("G0ABC", "GB7BBS", False)
    Digipeater(b, DigiConfig(enabled=True))
    accepted = accept_all(c)
    conn = a.connect("GB7BBS", via=["GM4BRI-1"])
    got_a, events = collect(conn)
    assert world.run(120, lambda: conn.connected), conn.state
    far, got_c, _ = accepted[0]
    assert [str(h) for h in far.via] == ["GM4BRI-1"]       # the reverse path
    data = bytes(range(256)) * 20
    conn.send(data)
    assert world.run(900, lambda: len(got_c) >= len(data))
    assert bytes(got_c) == data
    far.send(b"welcome to the BBS\r")
    assert world.run(120, lambda: bytes(got_a) == b"welcome to the BBS\r")
    assert b.digipeater.stats["repeated"] > 20
    conn.close()
    assert world.run(120, lambda: conn.state is State.DISCONNECTED and far.state is State.DISCONNECTED)
    # The first T1 allowed for the digipeater's two extra hops.
    assert conn.link.config.frack_s * 3 == pytest.approx(9.0)
