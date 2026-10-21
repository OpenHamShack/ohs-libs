"""NET/ROM NODES broadcasts read back as sent, as far as they hold
together, and nothing else mistaken for one."""

from __future__ import annotations

from helpers import address, frame_bytes

from shack_packet import ax25
from shack_packet.ax25 import PID_NETROM
from shack_packet.netrom import Route, build_nodes, parse_nodes


def test_a_broadcast_built_by_hand_reads_back():
    # Built from the bytes, not from build_nodes: a test that builds with
    # the code that reads passes when both are wrong the same way.
    info = (b"\xff" + b"BBB   " + address("GB7AAA") + b"AAA   " + address("GB7BBB") + bytes([192])
            + address("GB7CCC", 1) + b"CCC   " + address("GB7BBB") + bytes([150]))
    frame = ax25.parse(frame_bytes("GB7BBB", "NODES", "", info_bytes=info, pid=PID_NETROM))
    got = parse_nodes(frame)
    assert (got.node, got.alias, got.truncated) == ("GB7BBB", "BBB", False)
    assert got.routes == [Route("GB7AAA", "AAA", "GB7BBB", 192), Route("GB7CCC-1", "CCC", "GB7BBB", 150)]
    assert build_nodes("GB7BBB", "BBB", got.routes).info == frame.info


def test_a_short_or_broken_route_is_kept_as_far_as_it_was_good():
    whole = build_nodes("GB7BBB", "BBB", [Route("GB7AAA", "AAA", "GB7BBB", 192)]).encode()
    cut = ax25.parse(whole + b"\x00" * 9)
    got = parse_nodes(cut)
    assert len(got.routes) == 1 and got.truncated
    broken = ax25.parse(whole[:-21] + b"\x01" * 21)               # an address with its end bit inside
    got = parse_nodes(broken)
    assert got.routes == [] and got.truncated


def test_other_frames_are_not_broadcasts():
    assert parse_nodes(ax25.parse(frame_bytes("GB7BBB", "NODES", "\xffBBB   ", pid=0xF0))) is None
    assert parse_nodes(ax25.parse(frame_bytes("GB7BBB", "ID", "\xffBBB   ", pid=PID_NETROM))) is None
    assert parse_nodes(ax25.parse(frame_bytes("GB7BBB", "NODES", "BBB   ", pid=PID_NETROM))) is None


def test_the_node_table_ranks_direct_nodes_first_and_keeps_the_best_route():
    from shack_packet.netrom import DEFAULT_PATH_QUALITY, NodeTable
    from shack_packet.terminal import NodeEntry, RouteEntry
    table = NodeTable()
    aaa = build_nodes("GB7AAA", "AAA", [Route("GB7CCC", "CCC", "GB7XXX", 200), Route("GB7AAA", "AAA", "X", 255)])
    bbb = build_nodes("GB7BBB", "BBB", [Route("GB7CCC", "CCC", "GB7YYY", 100)])
    assert table.heard_frame(ax25.parse(aaa.encode()), 10.0)
    assert table.heard_frame(ax25.parse(bbb.encode()), 11.0)
    assert not table.heard_frame(ax25.parse(frame_bytes("G0ABC", "APRS", ">hi")), 12.0)
    ccc = table.find("CCC")
    # Reached through the broadcaster that offered the better route, at
    # the quality a node would work out: advertised x path quality / 256.
    assert (ccc.call, ccc.neighbour, ccc.quality, ccc.direct) == ("GB7CCC", "GB7AAA", 200 * DEFAULT_PATH_QUALITY // 256,
                                                                 False)
    # A broadcaster's own entry in its list does not make it indirect.
    assert table.find("GB7AAA").direct and table.find("GB7AAA").neighbour == ""
    # The same neighbour's worse news still replaces what it said before.
    worse = build_nodes("GB7AAA", "AAA", [Route("GB7CCC", "CCC", "GB7XXX", 10)])
    table.heard_frame(ax25.parse(worse.encode()), 13.0)
    assert table.find("GB7CCC").quality == 10 * DEFAULT_PATH_QUALITY // 256
    # A listing printed by a node we are connected to adds what it knows.
    table.listed("GB7BBB", nodes=[NodeEntry("DDD", "GB7DDD"), NodeEntry("BBB", "GB7BBB")],
                 routes=[RouteEntry(1, "GB7EEE-1", 150, 4)], now=14.0)
    assert table.find("DDD").neighbour == "GB7BBB" and table.find("GB7EEE-1").quality == 150
    assert [n.call for n in table.ranked()][:2] == ["GB7AAA", "GB7BBB"]
    assert table.find("GB7BBB").direct
