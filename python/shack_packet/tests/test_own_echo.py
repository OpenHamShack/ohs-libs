"""A station whose radio hears its own transmissions (an SDR: a Pluto
receives all through its own overs) ignores its own frames - found on air
18/10/2026, when GM4BRI-2 on a Pluto answered its own SABM to GM4BRI-1 with
a DM (both are "its" callsign at some SSID) and never connected."""

from __future__ import annotations

from stations import World, accept_all, collect

from shack_packet.channel import OWN_ECHO_S


def test_a_station_that_hears_itself_connects_to_another_ssid_of_its_own_call():
    w = World()
    a = w.station("GM4BRI-1")
    b = w.station("GM4BRI-2", hears_itself=True)
    accepted = accept_all(a)
    conn = b.connect("GM4BRI-1")
    got, events = collect(conn)
    assert w.run(30, until=lambda: conn.connected)
    conn.send(b"hello over the Pluto\r")
    assert w.run(30, until=lambda: accepted and bytes(accepted[0][1]) == b"hello over the Pluto\r")
    assert b.stats["own_echo"] >= 2                    # its SABM and its I frame came back, and were let be
    assert a.stats["own_echo"] == 0
    # Nothing it sent was answered by itself: no DM from GM4BRI-1 ever left it.
    assert b.stats["refused"] == 0


def test_without_the_echo_check_the_on_air_failure_comes_back(monkeypatch):
    # The same world with the check switched off reproduces 18/10/2026: B
    # refuses its own SABM, and the connection does not come up.
    from shack_packet import channel
    monkeypatch.setattr(channel.ChannelAccess, "own_echo", lambda self, frame, now: False)
    w = World()
    a = w.station("GM4BRI-1")
    b = w.station("GM4BRI-2", hears_itself=True)
    accept_all(a)
    conn = b.connect("GM4BRI-1")
    w.run(30, until=lambda: conn.connected)
    assert not conn.connected and b.stats["refused"] >= 1


def test_a_digipeated_copy_of_our_frame_is_still_heard():
    from shack_packet.ax25 import Frame
    w = World()
    b = w.station("GM4BRI-2", hears_itself=True)
    raw = Frame.from_monitor("GM4BRI-2>APZOHS,WIDE1-1:>beacon").encode()
    b.channel._own.append((w.clock(), raw))
    digied = Frame.from_monitor("GM4BRI-2>APZOHS,MB7UC*,WIDE1*:>beacon").encode()
    assert b.channel.own_echo(raw, w.clock())
    assert not b.channel.own_echo(digied, w.clock())
    assert not b.channel.own_echo(raw, w.clock() + OWN_ECHO_S + 1)    # forgotten in time
