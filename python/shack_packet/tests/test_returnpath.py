"""A connection with one RF direction and its answers by return path
(returnpath.py): two engines on the simulated channel, the receiver with
transmit off, joined by a LoopReturnPath."""

from __future__ import annotations

import random

import pytest
from stations import World, accept_all, collect

from shack_packet.ax25 import Control, Frame, FrameType
from shack_packet.datalink import LinkConfig, State
from shack_packet.errors import PacketError
from shack_packet.returnpath import LoopReturnPath

SENDER, RECEIVER = "GM4BRI", "GM0OTB"


def _payload(size, seed=7):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


def one_way(latency_s=0.08, jitter_s=0.0, loss=0.0, seed=1, **medium):
    """GM4BRI transmits; GM0OTB only receives (transmit off, as on the test,
    where its antenna is on the RX port) and answers by return path."""
    world = World(**medium)
    link = LinkConfig(modulo=128, k=16)
    a = world.station(SENDER, link=link)
    b = world.station(RECEIVER, link=link, tx=False)
    rx_end, tx_end = LoopReturnPath.pair(SENDER, RECEIVER, latency_s=latency_s, clock=world.clock,
                                         jitter_s=jitter_s, loss=loss, seed=seed)
    b.attach_return_path(rx_end)
    a.attach_return_path(tx_end)
    taps = {SENDER: [], RECEIVER: []}
    a.taps.append(lambda kind, d: taps[SENDER].append((kind, d)))
    b.taps.append(lambda kind, d: taps[RECEIVER].append((kind, d)))
    return world, a, b, rx_end, tx_end, taps


def test_a_transfer_completes_with_only_one_rf_direction():
    world, a, b, rx_end, _tx_end, taps = one_way()
    accepted = accept_all(b)
    conn = a.connect(RECEIVER)
    assert world.run(60, lambda: conn.connected)
    data = _payload(20_000)
    conn.send(data)
    far, got, _ = accepted[0]
    assert world.run(900, lambda: bytes(got) == data and conn.pending == 0)
    # The receiver never keyed: everything it said went by return path,
    # and the sender heard all of it there and nothing on the air.
    assert b.channel.stats.transmissions == 0 and b.channel.stats.refused_frames == 0
    assert b.stats["rp_sent"] == len(rx_end.sent) > 0
    assert a.stats["rp_heard"] == b.stats["rp_sent"]
    assert {d["via"] for k, d in taps[SENDER] if k == "heard"} == {"internet"}
    sent = {d["frame"].type for k, d in taps[RECEIVER] if k == "return-path"}
    assert FrameType.UA in sent and FrameType.RR in sent and FrameType.I not in sent
    # The receiver heard the sender's I frames on the air, marked so.
    assert {d["via"] for k, d in taps[RECEIVER] if k == "heard"} == {"radio"}
    assert far.connected and conn.return_path is not None and far.return_path is rx_end


def test_answers_wait_for_the_end_of_the_burst():
    """One RR a burst, not one per frame: the receiver answers once it has
    heard no carrier for RETURN_SETTLE_S."""
    world, a, b, rx_end, _tx_end, _taps = one_way(mode="audio")
    accept_all(b)
    conn = a.connect(RECEIVER)
    assert world.run(60, lambda: conn.connected)
    conn.send(_payload(4 * 256))
    assert world.run(300, lambda: conn.pending == 0)
    rrs = [f for f in rx_end.sent if _type(f) is FrameType.RR]
    assert 1 <= len(rrs) <= 2


def _type(raw):
    from shack_packet.ax25 import parse
    return parse(raw, 128).type


def test_without_a_return_path_the_receiver_cannot_answer():
    world = World()
    a = world.station(SENDER, link=LinkConfig(n2=2))
    b = world.station(RECEIVER, tx=False)
    accept_all(b)
    conn = a.connect(RECEIVER)
    _, events = collect(conn)
    assert world.run(120, lambda: conn.state is State.DISCONNECTED)
    assert events[-1].reason == "timeout"
    assert b.channel.stats.transmissions == 0 and b.channel.stats.refused_frames > 0


def test_losing_it_drops_both_ends_silently_and_the_sender_stops():
    world, a, b, _rx_end, tx_end, _taps = one_way()
    accepted = accept_all(b)
    conn = a.connect(RECEIVER)
    _, events = collect(conn)
    assert world.run(60, lambda: conn.connected)
    conn.send(_payload(50_000))
    far, got, _ = accepted[0]
    assert world.run(300, lambda: len(got) > 5_000)
    tx_end.cut("relay-closed")
    assert conn.state is State.DISCONNECTED and far.state is State.DISCONNECTED
    assert events[-1].reason == "return-path-lost"
    lost = [e for e in a.events if e.kind == "return-path" and e.reason == "lost"]
    assert lost and lost[0].detail["why"] == "relay-closed" and lost[0].detail["links"] == 1
    # At most the over already under way finishes; then the radio is quiet.
    world.run(1)
    overs = a.channel.stats.transmissions
    world.run(120)
    assert a.channel.stats.transmissions == overs and not a.channel.transmitting
    assert b.channel.stats.transmissions == 0
    with pytest.raises(PacketError):
        a.connect(RECEIVER)


def test_only_the_paths_own_station_is_taken_from_it():
    world, a, _b, _rx_end, tx_end, _taps = one_way()
    stranger = Frame.build("G0ABC", SENDER, Control(FrameType.SABM, True), command=True).encode()
    tx_end._deliver(stranger)
    to_someone_else = Frame.build(RECEIVER, "M0XYZ", Control(FrameType.SABM, True), command=True).encode()
    tx_end._deliver(to_someone_else)
    assert a.stats["rp_refused"] == 2 and a.stats["rp_heard"] == 0 and not a.connections


def test_none_by_default_and_attaching_is_an_event():
    world = World()
    a = world.station(SENDER)
    assert a.return_paths == {}
    rx_end, _ = LoopReturnPath.pair(RECEIVER, SENDER, clock=world.clock)
    a.attach_return_path(rx_end)
    assert [(e.kind, e.reason) for e in a.events] == [("return-path", "on")]
    assert a.events[0].detail == {"name": "internet", "remote": RECEIVER, "outbound": True, "up": True}
    a.detach_return_path(RECEIVER)
    assert a.return_paths == {} and a.events[-1].reason == "off"


@pytest.mark.parametrize("latency_s", [0.3, 0.8, 1.5])
def test_the_web_relays_latency_jitter_and_loss_are_survived(latency_s):
    """openhamshack.com's relay (weblink.py) is HTTPS long polls: an answer
    takes 0.3-1.5 s and varies, and now and then a batch is refused. AX.25
    must still finish, and T1 must stay above the return trip: before
    20/10/2026 acknowledgements arriving while the next over was queued
    were measured as round trips of zero (datalink._stop_t1_measured), SRT
    fell to 0.67 s and T1 to 1.35 s, under a 1.5 s relay."""
    jitter = latency_s / 2
    world, a, b, rx_end, tx_end, _taps = one_way(latency_s=latency_s, jitter_s=jitter, loss=0.03, seed=5)
    accepted = accept_all(b)
    conn = a.connect(RECEIVER)
    assert world.run(120, lambda: conn.connected)
    data = _payload(20 * 1024)
    conn.send(data)
    far, got, _ = accepted[0]
    assert world.run(3000, lambda: bytes(got) == data and conn.pending == 0)
    link = conn.link
    assert link.t1v > latency_s + jitter
    assert link.srt >= 1.0
    # Go-back-N after a REJ that arrives late resends what was already on
    # its way, so a slow relay costs repeats (about 60 of 80 frames at
    # 1.5 s, against 9 at 80 ms); bounded, so a T1 that expires on every
    # over (each window sent several times) would fail here.
    assert link.stats.get("retransmitted", 0) < link.stats["sent_I"] / 2 + 1
    assert rx_end.lost_frames + tx_end.lost_frames > 0
    assert b.channel.stats.transmissions == 0
